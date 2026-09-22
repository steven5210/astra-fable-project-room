"""Synthetic AO storage fixtures for the separate Codex owner reader: no real AO database or native model is used."""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

import ao_native_identity as native


class CodexOwnerFixture(unittest.TestCase):
    """Builds the same three-table AO owner schema NativeOwnerTests.setUp uses in
    test_ao_native_identity.py, parameterized so each test overrides only the
    column it is exercising. That existing test file is not modified."""
    SESSION_ID = 'reviewer'

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.build()

    def build(self, path=None, session=None, conversation=None, branch=None):
        path = Path(path) if path is not None else (self.root / 'ao.db')
        session = {'id': self.SESSION_ID, 'project_id': 'project', 'harness': 'codex', 'session_mode': 'chat',
                   'is_terminated': 0, 'activity_state': 'idle', 'workspace_path': '/synthetic/workspace',
                   'provider_conversation_id': 'codex-native-uuid', 'controller_generation': 'generation-one',
                   **(session or {})}
        conversation = {'id': 'conversation', 'current_session_id': self.SESSION_ID, 'active_branch_id': 'branch',
                        **(conversation or {})}
        branch = {'id': 'branch', 'conversation_id': 'conversation',
                  'provider_conversation_id': session['provider_conversation_id'], 'session_id': self.SESSION_ID,
                  'strategy': 'native', 'replay_truncated': 0, **(branch or {})}
        with sqlite3.connect(path) as db:
            db.executescript('''CREATE TABLE sessions(id,project_id,harness,session_mode,is_terminated,
              activity_state,workspace_path,provider_conversation_id,controller_generation);
              CREATE TABLE conversations(id,current_session_id,active_branch_id);
              CREATE TABLE conversation_branches(id,conversation_id,provider_conversation_id,session_id,strategy,replay_truncated);''')
            db.execute('INSERT INTO sessions VALUES(:id,:project_id,:harness,:session_mode,:is_terminated,'
                       ':activity_state,:workspace_path,:provider_conversation_id,:controller_generation)', session)
            db.execute('INSERT INTO conversations VALUES(:id,:current_session_id,:active_branch_id)', conversation)
            db.execute('INSERT INTO conversation_branches VALUES(:id,:conversation_id,:provider_conversation_id,'
                       ':session_id,:strategy,:replay_truncated)', branch)
        os.chmod(path, 0o644)
        return path


class CodexOwnerAcceptedTests(CodexOwnerFixture):
    def test_valid_codex_row_returns_exact_query_columns(self):
        owner = native.read_codex_owner(self.path, self.SESSION_ID)
        self.assertEqual(set(owner), set(native.QUERY_COLUMNS))
        self.assertEqual(owner['harness'], 'codex')
        self.assertEqual(owner['id'], self.SESSION_ID)

    def test_read_owner_and_read_codex_owner_refuse_the_other_harness_both_directions(self):
        with self.assertRaisesRegex(ValueError, 'AO storage does not prove a retained native Claude owner'):
            native.read_owner(self.path, self.SESSION_ID)
        claude_path = self.build(path=self.root / 'claude.db', session={'harness': 'claude-code'})
        self.assertEqual(native.read_owner(claude_path, self.SESSION_ID)['harness'], 'claude-code')
        with self.assertRaisesRegex(ValueError, 'AO storage does not prove a retained native Codex owner'):
            native.read_codex_owner(claude_path, self.SESSION_ID)

    def test_empty_and_native_strategy_are_accepted_unknown_nonempty_is_not(self):
        for strategy in ('', 'native'):
            with self.subTest(strategy=strategy):
                path = self.build(path=self.root / ('strategy-' + (strategy or 'empty') + '.db'),
                                  branch={'strategy': strategy})
                self.assertEqual(native.read_codex_owner(path, self.SESSION_ID)['strategy'], strategy)
        for strategy in ('replay', 'rehydrated'):
            with self.subTest(strategy=strategy):
                path = self.build(path=self.root / ('strategy-' + strategy + '.db'), branch={'strategy': strategy})
                with self.assertRaisesRegex(ValueError, 'AO storage does not prove a retained native Codex owner'):
                    native.read_codex_owner(path, self.SESSION_ID)

    def test_read_is_read_only_and_leaves_bytes_and_mtime_unchanged(self):
        before_bytes = self.path.read_bytes()
        before_stat = self.path.stat()
        owner = native.read_codex_owner(self.path, self.SESSION_ID)
        self.assertEqual(owner['harness'], 'codex')
        after_stat = self.path.stat()
        self.assertEqual(self.path.read_bytes(), before_bytes)
        self.assertEqual(after_stat.st_mtime_ns, before_stat.st_mtime_ns)
        self.assertEqual(after_stat.st_size, before_stat.st_size)


class CodexOwnerRefusalTests(CodexOwnerFixture):
    def test_relative_path_refuses(self):
        with self.assertRaisesRegex(ValueError, 'Use the verified absolute AO database path without symlinks'):
            native.read_codex_owner('relative-ao.db', self.SESSION_ID)

    def test_symlinked_database_path_refuses(self):
        link = self.root / 'link.db'
        link.symlink_to(self.path)
        with self.assertRaisesRegex(ValueError, 'Use the verified absolute AO database path without symlinks'):
            native.read_codex_owner(link, self.SESSION_ID)

    def test_group_or_world_writable_database_refuses(self):
        for mode in (0o664, 0o646):
            with self.subTest(mode=oct(mode)):
                os.chmod(self.path, mode)
                try:
                    with self.assertRaisesRegex(ValueError, 'AO database ownership is unsafe'):
                        native.read_codex_owner(self.path, self.SESSION_ID)
                finally:
                    os.chmod(self.path, 0o644)

    def test_non_regular_file_refuses(self):
        directory = self.root / 'not-a-file.db'
        directory.mkdir()
        with self.assertRaisesRegex(ValueError, 'AO database ownership is unsafe'):
            native.read_codex_owner(directory, self.SESSION_ID)

    def test_missing_row_refuses(self):
        with self.assertRaisesRegex(ValueError, 'AO native owner evidence is missing or ambiguous'):
            native.read_codex_owner(self.path, 'unknown-session')

    def test_terminated_session_refuses(self):
        path = self.build(path=self.root / 'terminated.db', session={'is_terminated': 1})
        with self.assertRaisesRegex(ValueError, 'AO storage does not prove a retained native Codex owner'):
            native.read_codex_owner(path, self.SESSION_ID)

    def test_non_chat_session_mode_refuses(self):
        path = self.build(path=self.root / 'session-mode.db', session={'session_mode': 'batch'})
        with self.assertRaisesRegex(ValueError, 'AO storage does not prove a retained native Codex owner'):
            native.read_codex_owner(path, self.SESSION_ID)

    def test_replay_truncated_refuses(self):
        path = self.build(path=self.root / 'replay-truncated.db', branch={'replay_truncated': 1})
        with self.assertRaisesRegex(ValueError, 'AO storage does not prove a retained native Codex owner'):
            native.read_codex_owner(path, self.SESSION_ID)

    def test_empty_or_whitespace_controller_generation_refuses(self):
        for value in ('', '   '):
            with self.subTest(length=len(value)):
                path = self.build(path=self.root / ('generation-' + str(len(value)) + '.db'),
                                  session={'controller_generation': value})
                with self.assertRaisesRegex(ValueError, 'AO storage does not prove a retained native Codex owner'):
                    native.read_codex_owner(path, self.SESSION_ID)

    def test_branch_provider_conversation_id_mismatch_refuses(self):
        path = self.build(path=self.root / 'branch-provider.db', branch={'provider_conversation_id': 'other-uuid'})
        with self.assertRaisesRegex(ValueError, 'AO storage does not prove a retained native Codex owner'):
            native.read_codex_owner(path, self.SESSION_ID)

    def test_branch_session_id_mismatch_refuses(self):
        path = self.build(path=self.root / 'branch-session.db', branch={'session_id': 'someone-else'})
        with self.assertRaisesRegex(ValueError, 'AO storage does not prove a retained native Codex owner'):
            native.read_codex_owner(path, self.SESSION_ID)

    def test_empty_project_id_or_workspace_path_refuses(self):
        for key in ('project_id', 'workspace_path'):
            with self.subTest(field=key):
                path = self.build(path=self.root / ('empty-' + key + '.db'), session={key: ''})
                with self.assertRaisesRegex(ValueError, 'AO storage does not prove a retained native Codex owner'):
                    native.read_codex_owner(path, self.SESSION_ID)

    def test_schema_without_expected_tables_refuses(self):
        path = self.root / 'no-schema.db'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE unrelated(id)')
        os.chmod(path, 0o644)
        with self.assertRaisesRegex(ValueError, 'AO owner storage is unavailable or its schema changed'):
            native.read_codex_owner(path, self.SESSION_ID)


if __name__ == '__main__':
    unittest.main()
