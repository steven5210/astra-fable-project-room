"""Private delegate copy/exec lifetime: synthetic preparation and MCP handshake only."""

import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import ao_delegates
import deepseek_adapter
import project_room
from test_ao_normal import DelegateFixture


ROOT = Path(__file__).resolve().parent
# Python imports this test-only guard at both launcher startup and adapter exec.
# The pinned launcher, adapter and command arguments remain unmodified.
GUARD = r'''
import json, os, sys
probe = json.loads(os.environ['DELEGATE_LIFETIME_PROBE'])
entry = os.path.realpath(sys.argv[0])
launcher = probe['launcher']
allowed_git = [
    ['git', '-C', probe['worktree'], 'rev-parse', '--show-toplevel'],
    ['git', '-C', probe['worktree'], 'rev-parse', '--path-format=absolute', '--git-common-dir'],
]
def record(kind, **values):
    with open(probe['events'], 'a', encoding='utf-8') as stream:
        stream.write(json.dumps({'kind': kind, **values}) + '\n')
def guard(event, args):
    if event.startswith('socket.') or event in ('os.system', 'os.posix_spawn', 'os.fork', 'os.forkpty'):
        raise RuntimeError('Delegate lifetime fixture forbids network/model execution')
    if event == 'subprocess.Popen':
        executable, argv, cwd, env = args
        if entry != launcher or executable != 'git' or list(argv) not in allowed_git or env is not None:
            raise RuntimeError('Delegate lifetime fixture permits only launcher Git identity reads')
        record('git', argv=list(argv))
    if event == 'os.exec':
        executable, argv, env = args
        if entry != launcher or executable != probe['server_argv'][0] or list(argv) != probe['server_argv']:
            raise RuntimeError('Delegate lifetime fixture permits only the exact retained adapter exec')
        record('exec', argv=list(argv))
    if event == 'open' and isinstance(args[0], (str, bytes)):
        path = os.path.realpath(os.fsdecode(args[0]))
        if os.path.basename(path) == probe['key_name']:
            raise RuntimeError('Delegate lifetime fixture forbids key reads')
        if any(path == root or path.startswith(root + os.sep) for root in probe['forbidden_sources']):
            raise RuntimeError('Delegate lifetime fixture forbids fallback to plugin source')
sys.addaudithook(guard)
record('startup', entry=entry, search_path=list(sys.path))
'''


class DelegateRuntimeLifetimeTests(DelegateFixture):
    def setUp(self):
        source_temp = tempfile.TemporaryDirectory(prefix='delegate-plugin-source-')
        self.addCleanup(source_temp.cleanup)
        self.source = Path(source_temp.name).resolve() / 'plugin-cache'
        self.source.mkdir()
        for name in ('ao_delegates.py', 'ao_delegate_launcher.py', 'deepseek_adapter.py'):
            shutil.copyfile(ROOT / name, self.source / name)
        # Exercise the actual copy operations, rather than hand-authoring a
        # preparation receipt or replacing the retained adapter with a fake.
        with mock.patch.object(project_room, 'ROOT', self.source), mock.patch.object(
                ao_delegates, '__file__', str(self.source / 'ao_delegates.py')):
            super().setUp()
            self.prepared = self.prepare()
        self.launcher = Path(self.prepared['launcher_path'])
        self.adapter = self.directory() / 'profiles' / 'deepseek_adapter.py'
        self.assertEqual(self.launcher.read_bytes(), (self.source / 'ao_delegate_launcher.py').read_bytes())
        self.assertEqual(self.adapter.read_bytes(), (self.source / 'deepseek_adapter.py').read_bytes())
        self.server = self.prepared['server']
        self.assertEqual(self.server['command'], sys.executable)
        self.assertEqual(self.server['args'][:2], [str(self.adapter), 'serve'])
        self.state_before = (self.directory() / 'state.json').read_bytes()
        self.guard_dir = self.root / 'lifetime-guard'
        self.guard_dir.mkdir()
        (self.guard_dir / 'sitecustomize.py').write_text(GUARD)
        self.events = self.root / 'lifetime-events.jsonl'
        self.isolated_home = self.root / 'isolated-user'
        self.isolated_home.mkdir()
        self.key = self.home / 'secrets' / 'synthetic-key'
        self.assertFalse(self.key.exists(), 'No credential bytes are needed for an MCP handshake')
        shutil.rmtree(self.source)
        self.assertFalse(self.source.exists())

    def run_launcher(self):
        probe = {'launcher': str(self.launcher), 'server_argv': [self.server['command'], *self.server['args']],
                 'worktree': str(self.repo), 'events': str(self.events), 'key_name': self.key.name,
                 'forbidden_sources': [str(self.source), str(ROOT)]}
        env = {'HOME': str(self.isolated_home), 'PATH': os.environ.get('PATH', os.defpath),
               'AO_SESSION_ID': 'engineer', 'PROJECT_ROOM_HOME': str(self.home),
               'PYTHONPATH': str(self.guard_dir), 'PYTHONNOUSERSITE': '1', 'PYTHONDONTWRITEBYTECODE': '1',
               'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_OPTIONAL_LOCKS': '0',
               'GIT_TERMINAL_PROMPT': '0', 'DELEGATE_LIFETIME_PROBE': json.dumps(probe)}
        messages = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-03-26'}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'ping', 'params': {}},
        ]
        payload = b''.join(json.dumps(message).encode() + b'\n' for message in messages)
        result = subprocess.run([sys.executable, '-B', str(self.launcher), '--home', str(self.home)],
                                cwd=self.repo, env=env, input=payload, capture_output=True, timeout=20)
        self.assertFalse(self.source.exists())
        self.assertFalse(self.key.exists())
        self.assertEqual((self.directory() / 'state.json').read_bytes(), self.state_before)
        events = [json.loads(line) for line in self.events.read_text().splitlines()]
        for event in events:
            if event['kind'] == 'startup':
                self.assertNotIn(str(ROOT), event['search_path'])
                self.assertNotIn(str(self.source), event['search_path'])
        return result, events

    def test_copied_launcher_execs_retained_adapter_after_plugin_cache_eviction(self):
        result, events = self.run_launcher()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(result.stderr, b'')
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([response['id'] for response in responses], [1, 2, 3])
        self.assertEqual(responses[0]['result']['serverInfo'],
                         {'name': deepseek_adapter.SERVER_NAME, 'version': deepseek_adapter.VERSION})
        self.assertEqual(responses[0]['result']['protocolVersion'], '2025-03-26')
        self.assertEqual({tool['name'] for tool in responses[1]['result']['tools']}, set(deepseek_adapter.TOOLS))
        self.assertEqual(responses[2]['result'], {})
        self.assertEqual([event['entry'] for event in events if event['kind'] == 'startup'],
                         [str(self.launcher), str(self.adapter)])
        self.assertEqual([event['argv'] for event in events if event['kind'] == 'exec'],
                         [[self.server['command'], *self.server['args']]])
        self.assertEqual(len([event for event in events if event['kind'] == 'git']), 2)
        launch = json.loads((self.directory() / 'delegate-launch.json').read_text())
        self.assertEqual(launch['session_id'], 'engineer')
        self.assertEqual(launch['preparation_sha256'], self.state()['preparation_sha256'])
        with sqlite3.connect((self.home / 'deepseek' / 'ledger.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)
        self.assertEqual(list((self.home / 'deepseek' / 'jobs').iterdir()), [])
        self.assertEqual(list((self.home / 'deepseek' / 'probes').iterdir()), [])

    def assert_target_refused(self):
        result, events = self.run_launcher()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b'')
        self.assertIn(b'Project Room delegate launch refused:', result.stderr)
        self.assertEqual([event['entry'] for event in events if event['kind'] == 'startup'], [str(self.launcher)])
        self.assertFalse(any(event['kind'] == 'exec' for event in events))
        self.assertFalse((self.directory() / 'delegate-launch.json').exists())
        self.assertFalse((self.home / 'deepseek' / 'ledger.sqlite3').exists())
        return result

    def test_missing_retained_adapter_refuses_without_source_fallback_or_launch_receipt(self):
        self.adapter.unlink()
        self.assert_target_refused()

    def test_changed_retained_adapter_refuses_before_exec(self):
        with self.adapter.open('ab') as stream:
            stream.write(b'\n# Synthetic pinned-byte corruption.\n')
        result = self.assert_target_refused()
        self.assertIn(b'Retained delegate snapshot changed', result.stderr)


if __name__ == '__main__':
    unittest.main()
