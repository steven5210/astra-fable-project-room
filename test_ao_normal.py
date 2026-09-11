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
import ao_routing
import ao_routing_guard as routing_guard
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
        (self.repo / '.gitignore').write_text('.claude/\n')  # the canonical repository ignores Claude runtime configuration
        subprocess.run(['git', '-C', str(self.repo), 'add', '.'], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(self.repo), 'commit', '-m', 'fixture'], check=True, capture_output=True)
        self.home = self.root / 'state'
        # Hermetic Claude user settings and no inherited subagent knobs from the test host.
        self.claude_env = self.root / 'claude-env'; self.claude_env.mkdir()
        patcher = patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(self.claude_env)}); patcher.start(); self.addCleanup(patcher.stop)
        for key in list(os.environ):
            if key in ao_routing.RECORDED_ENV:
                del os.environ[key]
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
if args==['--version']:
    print('0.0-fake (Claude Code)'); sys.exit(0)
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

    def test_old_deepseek_room_without_routing_stays_readable(self):
        self.prepare()
        prepared = ao.read(self.directory() / 'preparation.json'); prepared.pop('routing')
        ao.atomic(self.directory() / 'preparation.json', prepared)
        state = self.state(); state['preparation_sha256'] = ao.digest(prepared); ao.atomic(self.directory() / 'state.json', state)
        shutil.rmtree(self.repo / '.claude')
        delegate = self.service.ao_room_status(self.room)['delegate']
        self.assertEqual((delegate['attachment'], delegate['routing']['status']), ('configuration_verified', 'not_configured'))
        self.service.ao_room_bind(self.room, 'engineer', 'engineer', ao_workflow.FABLE_MODEL, 'max')

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


class RoutingTests(Fixture):
    """Native delegation routing: generated files, guard decisions, drift, old rooms and prelaunch refusal."""

    def setUp(self):
        super().setUp()
        self.fake_claude = self.root / 'fake-claude-version'
        self.fake_claude.write_text('#!' + sys.executable + '\nimport sys\nprint("0.0-fake (Claude Code)" if sys.argv[1:] == ["--version"] else "unexpected")\n')
        self.fake_claude.chmod(0o700)
        ao.atomic(self.home / 'config.json', {'claude_bin': str(self.fake_claude), 'claude_config_dir': str(self.claude_env)})
        self.room = self.open(provider='none'); self.spec()

    def prepared(self):
        return ao.read(self.directory() / 'preparation.json')

    def routing(self):
        return self.service.ao_room_status(self.room)['delegate']['routing']

    def decide(self, event):
        return routing_guard.decide(event)

    def commit(self, message):
        # Stage only tracked changes so an ignore-rule edit never silently tracks the runtime files.
        subprocess.run(['git', '-C', str(self.repo), 'commit', '-q', '-a', '-m', message], check=True, capture_output=True)

    def test_prepare_writes_ignored_pinned_files_and_snapshot(self):
        before = ao.candidate_snapshot(self.repo)
        ignore_before = (self.repo / '.gitignore').read_bytes()
        exclude = self.repo / '.git' / 'info' / 'exclude'
        exclude_before = exclude.read_bytes() if exclude.exists() else None
        (self.claude_env / 'settings.json').write_text('{"permissions": {"defaultMode": "auto"}}')
        prepared = self.service.ao_room_prepare(self.room, str(self.repo))
        self.assertEqual(before, ao.candidate_snapshot(self.repo))  # ignored paths never enter the candidate
        self.assertEqual((self.repo / '.gitignore').read_bytes(), ignore_before)
        self.assertEqual(exclude.read_bytes() if exclude.exists() else None, exclude_before)
        self.assertEqual((self.claude_env / 'settings.json').read_text(), '{"permissions": {"defaultMode": "auto"}}')
        routing = prepared['routing']
        self.assertEqual(routing['agents'], {'pr-sonnet': 'claude-sonnet-5', 'pr-opus': 'claude-opus-5'})
        self.assertEqual(routing['browser_skill'], 'claude-in-chrome')
        self.assertEqual(routing['claude']['version'], '0.0-fake (Claude Code)')
        self.assertEqual(routing['claude']['path'], str(self.fake_claude))
        for name in ('pr-sonnet', 'pr-opus'):
            text = (self.repo / '.claude' / 'agents' / (name + '.md')).read_text()
            fields = ao_routing.parse_definition(text)
            self.assertEqual((fields['model'], fields['effort']), (ao_routing.MODELS[name], 'max'))
            self.assertIn('Agent', fields['disallowedTools'])
            try:
                import yaml
            except ImportError:
                yaml = None
            if yaml is not None:  # the frontmatter must also be valid YAML for Claude's own loader
                self.assertEqual(yaml.safe_load(text[4:text.find('\n---\n', 4)]), fields)
        self.assertNotIn('Skill', ao_routing.parse_definition((self.repo / '.claude' / 'agents' / 'pr-opus.md').read_text())['disallowedTools'])
        settings = json.loads((self.repo / '.claude' / 'settings.local.json').read_text())
        self.assertEqual(settings['env']['CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH'], '1')
        self.assertEqual(settings['env']['CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS'], '2')
        self.assertIn('Skill(code-review)', settings['permissions']['deny']); self.assertIn('Workflow', settings['permissions']['deny'])
        entry = settings['hooks']['PreToolUse'][0]
        self.assertEqual(entry['matcher'], 'Agent|Workflow|Task|Skill|SendMessage|Team.*|mcp__deepseek__.*|mcp__qwen-local__.*|mcp__project-room__.*')
        for name in ('pr-sonnet', 'pr-opus'):
            tools = ao_routing.parse_definition((self.repo / '.claude' / 'agents' / (name + '.md')).read_text())['disallowedTools']
            for denied in ('mcp__deepseek', 'mcp__deepseek__deepseek_submit', 'mcp__deepseek__deepseek_ask', 'mcp__qwen-local', 'mcp__project-room'):
                self.assertIn(denied, tools)
        command = entry['hooks'][0]['command']
        self.assertIn(routing['guard_path'], command); self.assertTrue(command.endswith('exit 2')); self.assertNotIn('--browser-skill', command)
        self.assertEqual(Path(routing['guard_path']).read_bytes(), Path(routing_guard.__file__).read_bytes())
        self.assertTrue(routing['rules']['clause_present'])
        self.assertEqual(routing['rules']['preserved_fields']['worker.model'], 'claude-fable-5-1')
        self.assertEqual(self.routing()['status'], 'configured')
        self.assertEqual(prepared, self.service.ao_room_prepare(self.room, str(self.repo)))  # idempotent
        self.assertEqual(self.state()['preparation_status'], 'configured')

    def test_status_is_offline(self):
        self.bind()
        gets = self.fake.gets
        self.service.ao_room_status(self.room)
        self.assertEqual(self.fake.gets, gets)

    def test_unignored_tracked_or_symlinked_paths_refuse_before_writing(self):
        (self.repo / '.gitignore').write_text(''); self.commit('drop ignore')
        with self.assertRaisesRegex(ao.RoomError, 'not ignored'):
            self.service.ao_room_prepare(self.room, str(self.repo))
        self.assertFalse((self.repo / '.claude').exists())
        self.assertTrue(list(self.directory().glob('routing-error-*.json')))
        self.assertIsNone(self.state().get('preparation'))
        self.assertEqual(self.routing()['status'], 'not_configured')
        helper = subprocess.run([sys.executable, str(Path(ao_delegates.__file__)), '--home', str(self.home), '--room', self.room],
                                cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(helper.returncode, 1); self.assertIn('not ignored', helper.stderr)  # the postCreate hook fails, so AO aborts the spawn
        (self.repo / '.gitignore').write_text('.claude/\n'); self.commit('restore ignore')
        (self.repo / '.claude' / 'agents').mkdir(parents=True)
        (self.repo / '.claude' / 'agents' / 'pr-sonnet.md').write_text('tracked')
        subprocess.run(['git', '-C', str(self.repo), 'add', '-f', '.claude/agents/pr-sonnet.md'], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(self.repo), 'commit', '-q', '-m', 'track a routing path'], check=True, capture_output=True)
        with self.assertRaisesRegex(ao.RoomError, 'tracked'):
            self.service.ao_room_prepare(self.room, str(self.repo))
        subprocess.run(['git', '-C', str(self.repo), 'rm', '-q', '-f', '.claude/agents/pr-sonnet.md'], check=True, capture_output=True)
        self.commit('untrack')
        outside = self.root / 'outside'; outside.mkdir()
        shutil.rmtree(self.repo / '.claude', ignore_errors=True)
        (self.repo / '.claude').symlink_to(outside)
        with self.assertRaisesRegex(ao.RoomError, 'symlink'):
            self.service.ao_room_prepare(self.room, str(self.repo))
        self.assertEqual(list(outside.iterdir()), [])
        (self.repo / '.claude').unlink(); (self.repo / '.claude').mkdir()
        (self.repo / '.claude' / 'agents').symlink_to(outside)
        with self.assertRaisesRegex(ao.RoomError, 'symlink'):
            self.service.ao_room_prepare(self.room, str(self.repo))
        self.assertEqual(list(outside.iterdir()), [])

    def test_conflicting_definition_is_refused_before_any_file_is_written(self):
        (self.repo / '.claude' / 'agents').mkdir(parents=True)
        (self.repo / '.claude' / 'agents' / 'pr-opus.md').write_text('---\nname: pr-opus\nmodel: inherit\n---\n')
        with self.assertRaisesRegex(ao.RoomError, 'differs'):
            self.service.ao_room_prepare(self.room, str(self.repo))
        self.assertFalse((self.repo / '.claude' / 'agents' / 'pr-sonnet.md').exists())
        self.assertFalse((self.repo / '.claude' / 'settings.local.json').exists())

    def test_guard_decisions_and_fail_closed_exit(self):
        allow = {'tool_name': 'Agent', 'tool_input': {'subagent_type': 'pr-sonnet', 'prompt': 'apply', 'description': 'x'}}
        self.assertIsNone(self.decide(allow))
        self.assertIsNone(self.decide({'tool_name': 'Agent', 'tool_input': {'subagent_type': 'pr-opus', 'prompt': 'review', 'run_in_background': False}}))
        self.assertIsNone(self.decide({**allow, 'agent_type': None}))
        denied = [
            {'tool_name': 'Agent', 'tool_input': {'subagent_type': 'pr-sonnet', 'model': 'fable'}},
            {'tool_name': 'Agent', 'tool_input': {'subagent_type': 'pr-sonnet', 'isolation': 'remote'}},
            {'tool_name': 'Agent', 'tool_input': {'subagent_type': 'pr-opus', 'resume': 'agent-1'}},
            {'tool_name': 'Agent', 'tool_input': {'prompt': 'no type'}},
            {'tool_name': 'Agent', 'tool_input': {'subagent_type': 'general-purpose'}},
            {'tool_name': 'Agent', 'tool_input': {'subagent_type': 'fork'}},
            {'tool_name': 'Agent', 'tool_input': {'subagent_type': 'pr-sonnet'}, 'agent_type': 'pr-opus'},
            {'tool_name': 'Agent', 'tool_input': {'subagent_type': 'pr-sonnet'}, 'agent_id': 'existing-child'},
            {'tool_name': 'Agent', 'tool_input': {'subagent_type': 'pr-sonnet'}, 'agent_type': ''},
            {'tool_name': 'Agent', 'tool_input': {'subagent_type': 'pr-sonnet'}, 'agent_id': ''},
            {'tool_name': 'Workflow', 'tool_input': {'script': 'x'}},
            {'tool_name': 'Task', 'tool_input': {'prompt': 'legacy'}},
            {'tool_name': 'TeamCreate', 'tool_input': {}},
            {'tool_name': 'TeamMessage', 'tool_input': {'to': 'x'}},
            {'tool_name': 'SendMessage', 'tool_input': {'to': 'pr-opus-unrelated', 'model': 'fable', 'message': 'x'}},
            {'tool_name': 'SendMessage', 'tool_input': {'to': 'pr-opus', 'message': 'x'}},
            {'tool_name': 'Skill', 'tool_input': {'skill': 'code-review'}},
            {'tool_name': 'Skill', 'tool_input': {'skill': 'claude-in-chrome'}},
            {'tool_name': 'Skill', 'tool_input': {'skill': 'claude-in-chrome'}, 'agent_id': 'child'},
            {'tool_name': 'Skill', 'tool_input': {'skill': 'claude-in-chrome'}, 'agent_type': ''},
            {'tool_name': 'Skill', 'tool_input': {'skill': 'claude-in-chrome'}, 'agent_type': 'pr-sonnet'},
            {'tool_name': 'Skill', 'tool_input': {'skill': 'simplify'}, 'agent_type': 'pr-opus'},
            {'tool_name': 'mcp__deepseek__deepseek_submit', 'tool_input': {'task': 'x', 'request_id': 'r'}, 'agent_type': 'pr-sonnet'},
            {'tool_name': 'mcp__deepseek__deepseek_ask', 'tool_input': {'question': 'x'}, 'agent_type': 'pr-opus'},
            {'tool_name': 'mcp__deepseek__deepseek_status', 'tool_input': {'job_id': 'j'}, 'agent_id': 'child'},
            {'tool_name': 'mcp__qwen-local__qwen_submit', 'tool_input': {'task': 'x'}, 'agent_type': 'pr-sonnet'},
            {'tool_name': 'mcp__project-room__room_implementation_submit', 'tool_input': {}, 'agent_type': 'pr-opus'},
            {'tool_name': 'mcp__project-room__ao_room_send', 'tool_input': {}, 'agent_id': 'child'},
        ]
        for event in denied:
            self.assertIsNotNone(self.decide(event), event)
        self.assertIsNone(self.decide({'tool_name': 'Skill', 'tool_input': {'skill': 'claude-in-chrome'}, 'agent_type': 'pr-opus', 'agent_id': 'child'}))
        self.assertIsNone(self.decide({'tool_name': 'Read', 'tool_input': {'file_path': 'x'}}))
        self.assertIsNone(self.decide({'tool_name': 'Read', 'tool_input': {'file_path': 'x'}, 'agent_type': 'pr-sonnet'}))
        self.assertIsNone(self.decide({'tool_name': 'TaskOutput', 'tool_input': {'task_id': 'x'}}))
        for root_tool in ('mcp__deepseek__deepseek_submit', 'mcp__deepseek__deepseek_ask', 'mcp__deepseek__deepseek_result', 'mcp__project-room__ao_room_status'):
            self.assertIsNone(self.decide({'tool_name': root_tool, 'tool_input': {'task': 'x'}}))  # root Fable keeps its pinned provider access
        for bad in ({'tool_name': 'Agent', 'tool_input': []}, {'tool_input': {}}, {'tool_name': 'Agent', 'tool_input': {'subagent_type': 'pr-sonnet'}, 'agent_type': 5}, 'text'):
            with self.assertRaises(ValueError):
                self.decide(bad)
        script = Path(routing_guard.__file__)
        run = lambda payload, *args: subprocess.run([sys.executable, str(script), *args], input=payload, capture_output=True, text=True)
        refused = run(json.dumps({'tool_name': 'Agent', 'tool_input': {'subagent_type': 'general-purpose'}}))
        self.assertEqual(refused.returncode, 0)
        self.assertEqual(json.loads(refused.stdout)['hookSpecificOutput']['permissionDecision'], 'deny')
        allowed = run(json.dumps(allow)); self.assertEqual((allowed.returncode, allowed.stdout), (0, ''))
        self.assertEqual(run('not json').returncode, 2)
        self.assertEqual(run(json.dumps(allow), '--browser-skill', 'code-review').returncode, 2)  # no arguments are accepted
        command = ao_routing.hook_command(str(self.root / 'missing-python'), str(script))
        wrapped = subprocess.run(['sh', '-c', command], input=json.dumps(allow), capture_output=True, text=True)
        self.assertEqual(wrapped.returncode, 2)
        command = ao_routing.hook_command(sys.executable, str(script))
        wrapped = subprocess.run(['sh', '-c', command], input='not json', capture_output=True, text=True)
        self.assertEqual(wrapped.returncode, 2)
        wrapped = subprocess.run(['sh', '-c', command], input=json.dumps(allow), capture_output=True, text=True)
        self.assertEqual((wrapped.returncode, wrapped.stdout), (0, ''))

    def test_definition_validation_rejects_omitted_inherit_and_fable_models(self):
        text = ao_routing.agent_definition('pr-sonnet')
        ao_routing.validate_definition('pr-sonnet', text)
        tools_line = 'disallowedTools: ' + json.dumps(ao_routing.parse_definition(text)['disallowedTools'])
        cases = [('"claude-sonnet-5"', '"inherit"', 'inherits'), ('"claude-sonnet-5"', '"claude-fable-5-1"', 'Fable'),
                 ('model: "claude-sonnet-5"\n', '', 'omits'), ('"claude-sonnet-5"', '"claude-opus-5"', 'pinned mapping'),
                 ('effort: "max"', 'effort: "high"', 'effort'),
                 (tools_line, 'disallowedTools: "Skill"', 'delegate'),
                 (tools_line, 'disallowedTools: "Agent, Workflow, SendMessage, ' + ao_routing.CHILD_DENIED + '"', 'skills'),
                 (tools_line, 'disallowedTools: "Agent, Workflow, Task, Skill, SendMessage, TeamCreate"', 'inherited MCP'),
                 (tools_line, 'disallowedTools: "Agent, Workflow, Task, Skill, SendMessage, TeamCreate, mcp__deepseek, mcp__qwen-local, mcp__project-room"', 'inherited MCP')]
        for old, new, message in cases:
            self.assertIn(old, text)
            with self.assertRaisesRegex(ao.RoomError, message):
                ao_routing.validate_definition('pr-sonnet', text.replace(old, new))
        with self.assertRaisesRegex(ao.RoomError, 'frontmatter'):
            ao_routing.validate_definition('pr-sonnet', 'no frontmatter')

    def test_local_drift_blocks_delegation_but_never_read_only_review(self):
        self.bind()
        self.assertEqual(self.routing()['status'], 'configured')
        path = self.repo / '.claude' / 'agents' / 'pr-opus.md'; original = path.read_bytes()
        drifted = original.replace(b'"claude-opus-5"', b'"inherit"')
        path.write_bytes(drifted)
        status = self.routing(); self.assertEqual(status['status'], 'unverified'); self.assertIn('changed', status['error'])
        self.assertEqual(self.service.ao_room_status(self.room)['delegate']['attachment'], 'configuration_verified')
        self.agree()  # Fable's read-only spec review is never blocked by routing drift
        with self.assertRaisesRegex(ao.RoomError, 'routing file changed'):
            self.service.ao_room_handoff(self.room, str(self.repo))
        path.write_bytes(original)
        self.assertEqual(self.routing()['status'], 'verified')  # the spec-review sync observed consistent rules
        self.service.ao_room_handoff(self.room, str(self.repo))
        path.write_bytes(drifted)
        with self.assertRaisesRegex(ao.RoomError, 'routing file changed'):
            self.send('implementation')
        self.assertNotIn('implementation', self.state()['requests'])
        path.write_bytes(original)
        self.implement()
        path.write_bytes(drifted)
        self.review()  # Astra's read-only acceptance review is never blocked either
        self.assertTrue(self.service.ao_room_accept(self.room, 'acceptance_review')['accepted'])
        with self.assertRaisesRegex(ao.RoomError, 'routing file changed'):
            self.send('correction')
        path.write_bytes(original)
        (self.repo / '.gitignore').write_text(''); self.commit('drop ignore after preparation')
        self.assertIn('not ignored', self.routing()['error'])
        (self.repo / '.gitignore').write_text('.claude/\n'); self.commit('restore ignore')
        for content, message in (({'env': {'CLAUDE_CODE_SUBAGENT_MODEL_FORCE': 'claude-fable-5-1'}}, 'override'),
                                 ({'availableModels': ['claude-fable-5-1', 'claude-sonnet-5']}, 'availableModels'),
                                 ({'modelOverrides': {'claude-opus-5': 'claude-fable-5-1'}}, 'modelOverrides'),
                                 ({'disableAllHooks': True}, 'disable hooks')):
            (self.claude_env / 'settings.json').write_text(json.dumps(content))
            self.assertIn(message, self.routing()['error'])
        (self.claude_env / 'settings.json').unlink()
        (self.claude_env / 'agents').mkdir(); (self.claude_env / 'agents' / 'pr-opus.md').write_text('---\nname: pr-opus\n---\n')
        self.assertIn('user-level agent definition', self.routing()['error'])
        shutil.rmtree(self.claude_env / 'agents')
        managed = self.root / 'managed-settings.json'
        managed.write_text(json.dumps({'env': {'CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS': '9'}}))
        with patch.dict(os.environ, {'CLAUDE_CODE_MANAGED_SETTINGS_PATH': str(managed)}):
            self.assertIn('managed', self.routing()['error'])
        (self.repo / '.claude' / 'settings.json').write_text(json.dumps({'disableAllHooks': True}))
        self.assertIn('project', self.routing()['error'])
        (self.repo / '.claude' / 'settings.json').unlink()
        self.assertEqual(self.routing()['status'], 'verified')
        self.fake_claude.write_text(self.fake_claude.read_text() + '# changed\n')
        self.assertIn('executable changed', self.routing()['error'])
        with self.assertRaisesRegex(ao.RoomError, 'executable changed'):
            self.send('correction', 'again')
        self.assertNotIn('again', self.state()['requests'])

    def test_rules_drift_blocks_implementation_and_sync_records_last_observation(self):
        self.bind(); self.agree(); self.service.ao_room_handoff(self.room, str(self.repo))
        self.fake.config['agentRules'] = 'Project Room rules without the clause'
        with self.assertRaisesRegex(ao.RoomError, 'rules changed'):
            self.send('implementation')
        rules = self.state()['routing_rules']
        self.assertEqual((rules['source'], rules['consistent'], rules['clause_present']), ('dispatch', False, False))
        self.assertTrue((self.directory() / rules['evidence']).exists())
        self.assertEqual(self.routing()['status'], 'unverified')
        self.service.ao_room_sync(self.room)  # a drifted sync still returns and records the inconsistency
        self.assertEqual((self.state()['routing_rules']['source'], self.state()['routing_rules']['consistent']), ('sync', False))
        self.fake.config['agentRules'] = ao_routing.CLAUSE + ' plus an edit'
        with self.assertRaisesRegex(ao.RoomError, 'rules changed'):
            self.send('implementation', 'impl-2')
        self.fake.config['agentRules'] = ao_routing.CLAUSE
        self.fake.config['worker']['agentConfig'].pop('model')
        with self.assertRaisesRegex(ao.RoomError, 'rules changed'):
            self.send('implementation', 'impl-3')
        self.fake.config['worker']['agentConfig']['model'] = 'claude-fable-5-1'
        self.fake.config['env'] = {'CLAUDE_CODE_SUBAGENT_MODEL_FORCE': 'claude-fable-5-1'}
        with self.assertRaisesRegex(ao.RoomError, 'rules changed'):
            self.send('implementation', 'impl-4')
        self.fake.config['env'] = {'CLAUDE_CODE_SUBAGENT_MODEL': 'claude-fable-5-1'}  # recorded env drift is drift too
        with self.assertRaisesRegex(ao.RoomError, 'rules changed'):
            self.send('implementation', 'impl-5')
        del self.fake.config['env']
        self.fake.fail_projects = True
        with self.assertRaisesRegex(ao.RoomError, 'could not be read'):
            self.send('implementation', 'impl-6')
        self.assertIn('outage', self.state()['routing_rules']['error'])
        self.service.ao_room_sync(self.room)
        self.assertIn('outage', self.state()['routing_rules']['error'])
        self.assertEqual(self.routing()['status'], 'unverified')
        self.fake.fail_projects = False
        self.assertNotIn('implementation', self.state()['requests'])
        self.service.ao_room_sync(self.room)
        self.assertEqual(self.routing()['status'], 'verified')
        evidence = self.directory() / self.state()['routing_rules']['evidence']
        saved = evidence.read_bytes()
        evidence.write_text('{"clause_present": true}')
        self.assertIn('evidence', self.routing()['error'])
        with self.assertRaisesRegex(ao.RoomError, 'modified'):
            self.service.ao_room_sync(self.room)  # a tampered receipt is an explicit refusal, not silent reuse
        evidence.unlink()
        self.assertEqual(self.routing()['status'], 'unverified')
        evidence.write_bytes(saved)
        self.assertEqual(self.routing()['status'], 'verified')
        self.implement()
        request = self.state()['requests']['implementation']
        self.assertIn('pr-sonnet (claude-sonnet-5', request['text']); self.assertIn('pr-opus (claude-opus-5', request['text'])
        self.review()
        self.assertTrue(self.service.ao_room_accept(self.room, 'acceptance_review')['accepted'])

    def test_missing_clause_refuses_preparation_and_wrapped_clause_counts(self):
        self.fake.config['agentRules'] = 'no clause'
        with self.assertRaisesRegex(ao.RoomError, 'delegation clause'):
            self.service.ao_room_prepare(self.room, str(self.repo))
        self.assertFalse((self.repo / '.claude').exists())
        self.assertTrue(list(self.directory().glob('routing-error-*.json')))
        self.fake.config['agentRules'] = 'Existing rules.\n' + ao_routing.CLAUSE.replace(' ', '\n', 5)
        self.service.ao_room_prepare(self.room, str(self.repo))
        self.assertEqual(self.routing()['status'], 'configured')

    def test_contradictory_settings_refuse_and_unrelated_local_settings_are_preserved(self):
        (self.claude_env / 'settings.json').write_text(json.dumps({'disableAllHooks': True}))
        with self.assertRaisesRegex(ao.RoomError, 'disable hooks'):
            self.service.ao_room_prepare(self.room, str(self.repo))
        (self.claude_env / 'settings.json').write_text('{')
        with self.assertRaisesRegex(ao.RoomError, 'not valid JSON'):
            self.service.ao_room_prepare(self.room, str(self.repo))
        (self.claude_env / 'settings.json').write_text(json.dumps({'permissions': {'defaultMode': 'auto'}, 'model': 'fable[1m]'}))
        if os.geteuid() != 0:
            (self.claude_env / 'settings.json').chmod(0)
            with self.assertRaisesRegex(ao.RoomError, 'unreadable'):
                self.service.ao_room_prepare(self.room, str(self.repo))
            (self.claude_env / 'settings.json').chmod(0o600)
        (self.claude_env / 'agents').mkdir(); (self.claude_env / 'agents' / 'pr-sonnet.md').write_text('---\nname: pr-sonnet\n---\n')
        with self.assertRaisesRegex(ao.RoomError, 'user-level agent definition'):
            self.service.ao_room_prepare(self.room, str(self.repo))
        shutil.rmtree(self.claude_env / 'agents')
        ao.atomic(self.home / 'config.json', {'claude_bin': str(self.fake_claude), 'claude_config_dir': str(self.claude_env),
                                              'claude_config_dir_override': str(self.root / 'elsewhere')})
        with self.assertRaisesRegex(ao.RoomError, 'disagree'):
            self.service.ao_room_prepare(self.room, str(self.repo))
        ao.atomic(self.home / 'config.json', {'claude_bin': str(self.fake_claude), 'claude_config_dir': str(self.claude_env)})
        local = self.repo / '.claude' / 'settings.local.json'; local.parent.mkdir()
        local.write_text(json.dumps({'unrelated': {'keep': True}, 'env': {'FOO': 'bar'}, 'permissions': {'allow': ['Bash(ls)']},
                                     'hooks': {'PreToolUse': [{'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': 'true'}]}]}}))
        user_settings = (self.claude_env / 'settings.json').read_bytes()
        self.service.ao_room_prepare(self.room, str(self.repo))
        self.assertEqual((self.claude_env / 'settings.json').read_bytes(), user_settings)
        merged = json.loads(local.read_text())
        self.assertEqual(merged['unrelated'], {'keep': True}); self.assertEqual(merged['env']['FOO'], 'bar')
        self.assertEqual(merged['permissions']['allow'], ['Bash(ls)'])
        self.assertEqual([entry['matcher'] for entry in merged['hooks']['PreToolUse']], ['Bash', ao_routing.MATCHER])
        self.assertEqual(self.routing()['status'], 'configured')
        other = self.root / 'worktree-two'
        subprocess.run(['git', '-C', str(self.repo), 'worktree', 'add', '-q', '-b', 'second', str(other)], check=True, capture_output=True)
        (other / '.claude').mkdir()
        (other / '.claude' / 'settings.local.json').write_text(json.dumps({'env': {'CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS': '8'}}))
        second = self.open(feature='second', provider='none')
        with self.assertRaisesRegex(ao.RoomError, 'override'):
            self.service.ao_room_prepare(second, str(other))
        self.assertEqual(json.loads((other / '.claude' / 'settings.local.json').read_text()), {'env': {'CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS': '8'}})

    def test_old_prepared_rooms_stay_readable_and_not_configured(self):
        self.bind()
        prepared = self.prepared(); prepared.pop('routing')  # a record written before native routing existed
        ao.atomic(self.directory() / 'preparation.json', prepared)
        state = self.state(); state['preparation_sha256'] = ao.digest(prepared); ao.atomic(self.directory() / 'state.json', state)
        shutil.rmtree(self.repo / '.claude')
        delegate = self.service.ao_room_status(self.room)['delegate']
        self.assertEqual(delegate['attachment'], 'configuration_verified'); self.assertEqual(delegate['routing']['status'], 'not_configured')
        self.agree(); self.service.ao_room_handoff(self.room, str(self.repo)); self.send('implementation')
        self.assertIn('not configured', self.state()['requests']['implementation']['text'])
        self.service.ao_room_sync(self.room)
        self.assertNotIn('routing_rules', self.state())
        self.assertEqual(self.routing()['status'], 'not_configured')

    def test_rooms_without_executable_evidence_never_reach_verified(self):
        (self.home / 'config.json').unlink()
        self.bind(); self.agree(); self.service.ao_room_handoff(self.room, str(self.repo))
        self.assertIsNone(self.prepared()['routing']['claude']['path'])
        self.service.ao_room_sync(self.room)
        status = self.routing()
        self.assertEqual(status['status'], 'configured'); self.assertIn('version evidence is missing', status['note'])

    def test_managed_hooks_only_refuses_preparation_and_delegation_but_not_review(self):
        managed = self.root / 'managed-settings.json'
        managed.write_text(json.dumps({'allowManagedHooksOnly': True}))
        with patch.dict(os.environ, {'CLAUDE_CODE_MANAGED_SETTINGS_PATH': str(managed)}):
            with self.assertRaisesRegex(ao.RoomError, 'managed hooks only'):
                self.service.ao_room_prepare(self.room, str(self.repo))
            self.assertFalse((self.repo / '.claude').exists())
        (self.claude_env / 'settings.json').write_text(json.dumps({'allowManagedHooksOnly': True}))
        with self.assertRaisesRegex(ao.RoomError, 'managed hooks only'):
            self.service.ao_room_prepare(self.room, str(self.repo))
        (self.claude_env / 'settings.json').unlink()
        (self.repo / '.claude').mkdir(); (self.repo / '.claude' / 'settings.local.json').write_text(json.dumps({'allowManagedHooksOnly': True}))
        with self.assertRaisesRegex(ao.RoomError, 'suppress local hooks'):
            self.service.ao_room_prepare(self.room, str(self.repo))
        (self.repo / '.claude' / 'settings.local.json').unlink()
        self.bind()
        self.assertEqual(self.routing()['status'], 'configured')
        with patch.dict(os.environ, {'CLAUDE_CODE_MANAGED_SETTINGS_PATH': str(managed)}):
            status = self.routing()
            self.assertEqual(status['status'], 'unverified'); self.assertIn('managed hooks only', status['error'])
            self.agree()  # read-only specification review is still allowed
            with self.assertRaisesRegex(ao.RoomError, 'managed hooks only'):
                self.service.ao_room_handoff(self.room, str(self.repo))
        self.service.ao_room_handoff(self.room, str(self.repo))
        with patch.dict(os.environ, {'CLAUDE_CODE_MANAGED_SETTINGS_PATH': str(managed)}):
            with self.assertRaisesRegex(ao.RoomError, 'managed hooks only'):
                self.send('implementation')
            self.assertNotIn('implementation', self.state()['requests'])
        self.implement()
        with patch.dict(os.environ, {'CLAUDE_CODE_MANAGED_SETTINGS_PATH': str(managed)}):
            self.review()  # read-only acceptance review is still allowed
            self.assertTrue(self.service.ao_room_accept(self.room, 'acceptance_review')['accepted'])

    def test_failed_version_probe_is_recorded_and_caps_status(self):
        self.fake_claude.write_text('#!' + sys.executable + '\nimport sys\nsys.exit(1)\n')
        self.bind(); self.agree(); self.service.ao_room_handoff(self.room, str(self.repo))
        claude = self.prepared()['routing']['claude']
        self.assertEqual((claude['version'], claude['error']), (None, 'version probe exit 1'))
        self.service.ao_room_sync(self.room)
        status = self.routing()
        self.assertEqual(status['status'], 'configured'); self.assertIn('version probe exit 1', status['note'])
        ao.atomic(self.home / 'config.json', {'claude_bin': 'claude', 'claude_config_dir': str(self.claude_env)})
        self.assertEqual(ao_routing.claude_evidence('claude')['error'], 'claude_bin is not an absolute path')
