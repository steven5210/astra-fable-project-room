"""Synthetic AO storage fixtures: no real AO database or native model is used."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
import ao_native_identity as native


class NativeOwnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name).resolve() / 'ao.db'
        with sqlite3.connect(self.path) as db:
            db.executescript('''CREATE TABLE sessions(id,project_id,harness,session_mode,is_terminated,
              activity_state,workspace_path,provider_conversation_id,controller_generation);
              CREATE TABLE conversations(id,current_session_id,active_branch_id);
              CREATE TABLE conversation_branches(id,conversation_id,provider_conversation_id,session_id,strategy,replay_truncated);
              INSERT INTO sessions VALUES('engineer','project','claude-code','chat',0,'idle','/synthetic/worktree','native-uuid','generation-before');
              INSERT INTO conversations VALUES('conversation','engineer','branch');
              INSERT INTO conversation_branches VALUES('branch','conversation','native-uuid','engineer','native',0);''')

    def test_resume_requires_same_uuid_and_new_generation(self):
        before = native.read_owner(self.path, 'engineer')
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE sessions SET controller_generation='generation-after'")
        after = native.read_owner(self.path, 'engineer')
        self.assertTrue(native.verify_resume(before, after)['retained_native_identity'])
        for changed in ({**after, 'provider_conversation_id': 'replacement'},
                        {**after, 'controller_generation': 'generation-before'}, {**after, 'workspace_path': '/another'}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                native.verify_resume(before, changed)

    def test_legacy_empty_strategy_normalizes_only_as_ao_defines(self):
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE conversation_branches SET strategy=''")
        before = native.read_owner(self.path, 'engineer')
        self.assertEqual(before['strategy'], '')
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE sessions SET controller_generation='generation-after'")
            db.execute("UPDATE conversation_branches SET strategy='native'")
        self.assertTrue(native.verify_resume(before, native.read_owner(self.path, 'engineer'))['retained_native_identity'])

    def test_read_only_owner_uses_committed_wal_snapshot_without_writing_database(self):
        writer = sqlite3.connect(self.path)
        self.addCleanup(writer.close)
        self.assertEqual(writer.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
        writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute("UPDATE sessions SET controller_generation='committed-wal-generation'")
        writer.commit()
        wal = self.path.with_name(self.path.name + '-wal')
        self.assertTrue(wal.exists())
        before = {p: p.read_bytes() for p in (self.path, wal)}
        writer.execute("UPDATE sessions SET controller_generation='uncommitted-generation'")
        observed = native.read_owner(self.path, 'engineer')
        self.assertEqual(observed['controller_generation'], 'committed-wal-generation')
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        writer.rollback()

    def test_missing_ambiguous_terminated_replay_and_uuid_drift_refuse(self):
        with self.assertRaises(ValueError):
            native.read_owner(self.path, 'missing')
        for statement in ("UPDATE sessions SET is_terminated=1",
                          "UPDATE sessions SET provider_conversation_id='replacement'",
                          "UPDATE conversation_branches SET strategy='rehydrated'",
                          'UPDATE conversation_branches SET replay_truncated=1',
                          "INSERT INTO conversations VALUES('conversation','engineer','branch')"):
            old = self.path.read_bytes()
            try:
                with sqlite3.connect(self.path) as db:
                    db.execute(statement)
                with self.subTest(statement=statement), self.assertRaises(ValueError):
                    native.read_owner(self.path, 'engineer')
            finally:
                self.path.write_bytes(old)

    def test_schema_loss_and_missing_database_are_not_reconstructed(self):
        missing = self.path.with_name('missing.db')
        with self.assertRaises(OSError):
            native.read_owner(missing, 'engineer')
        self.assertFalse(missing.exists())
        with sqlite3.connect(self.path) as db:
            db.execute('DROP TABLE conversation_branches')
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            native.read_owner(self.path, 'engineer')
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
