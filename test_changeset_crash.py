"""Process interruption and cooperating-writer tests with explicit pipe barriers.

These tests prove process-crash behavior, not hardware/power-loss durability.
"""
import copy
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import select
import signal
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from changeset_core import ChangesetError,canonical,digest_object
import changeset_faults as faults
import changeset_fs as fs
import changeset_journal as journal
import changeset_tool as tool
from test_changeset_workflow import Case,tree_snapshot

class CrashTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='changeset-crash-',dir='/private/tmp' if os.path.isdir('/private/tmp') else None)
        self.addCleanup(self.temp.cleanup)
        self.counter=0
    def case(self,conditions=False):
        self.counter+=1; base=Path(self.temp.name)/str(self.counter); base.mkdir()
        c=Case(base,conditions=conditions)
        try: c.prepare()
        except ChangesetError as exc:
            if exc.code=='unsupported_capability': self.skipTest('Native Linux trusted-xattr capability unavailable; no success claimed')
            raise
        return c
    def start_child(self,action,point,detail=None,hold=False):
        """The child announces the exact fault boundary before exit or barrier."""
        ready_r,ready_w=os.pipe(); release_r,release_w=os.pipe()
        pid=os.fork()
        if pid==0:
            os.close(ready_r); os.close(release_w)
            def hook(actual,fields):
                if actual==point and all(fields.get(k)==v for k,v in (detail or {}).items()):
                    os.write(ready_w,b'R')
                    if hold:
                        if os.read(release_r,1)!=b'G': os._exit(73)
                        faults.clear_hook()
                    else: os._exit(71)
            faults.set_hook(hook)
            try:
                result=action()
                os.write(ready_w,b'D' if result.get('outcome') in ('applied_unverified','resumed_unverified','already_completed') else b'X')
                os._exit(0)
            except BaseException:
                os.write(ready_w,b'E'); os._exit(72)
        os.close(ready_w); os.close(release_r)
        def cleanup():
            try:
                waited,_=os.waitpid(pid,os.WNOHANG)
                if not waited:
                    os.kill(pid,signal.SIGKILL); os.waitpid(pid,0)
            except (ChildProcessError,ProcessLookupError): pass
            for fd in (ready_r,release_w):
                try: os.close(fd)
                except OSError: pass
        self.addCleanup(cleanup)
        self.assertTrue(select.select([ready_r],[],[],15)[0],'child failed to reach explicit boundary')
        self.assertEqual(os.read(ready_r,1),b'R','child failed before requested boundary')
        return pid,ready_r,release_w
    def crash(self,action,point,detail=None):
        pid,ready,release=self.start_child(action,point,detail)
        self.assertTrue(select.select([ready],[],[],15)[0],'crashed child did not close its pipe')
        self.assertEqual(os.read(ready,1),b'')
        self.assertEqual(os.waitstatus_to_exitcode(os.waitpid(pid,0)[1]),71)
    def verify_backups(self,c):
        chain=c.chain()
        for entry in chain['targets']:
            self.assertEqual((c.state/'backups'/entry['backup_sha256']).read_bytes(),c.before[entry['target']])
    def assert_cli_refusal(self,c,command,expected):
        args=[command,'--workspace',str(c.workspace),'--state-root',str(c.state)]
        if command!='audit': args+=['--input-dir',str(c.input),'--plan-sha256',c.sha,'--receipts','receipt.json']
        if command=='resume': args+=['--audit-sha256','0'*64]
        output=io.StringIO()
        with redirect_stdout(output): self.assertEqual(tool.main(args),2)
        raw=output.getvalue(); self.assertEqual(len(raw.splitlines()),1); result=json.loads(raw)
        self.assertEqual(result['outcome'],'refused')
        self.assertIn(result['error']['code'],expected if isinstance(expected,tuple) else (expected,))
        for private in (str(c.base),'alpha','bravo','steady'):
            self.assertNotIn(private,raw)
    def test_process_boundaries_new_apply_vs_explicit_resume(self):
        points=[('after_stage_create',{'target_index':0},False),('after_stage_chunk',{'target_index':0},False),
                ('before_prepared_write',None,False),('before_record_commit',{'record':'prepared.json'},False),
                ('after_prepared_write',None,True),('before_rename',{'target_index':0},True),
                ('after_rename',{'target_index':0},True),('after_rename',{'target_index':1},True),
                ('before_completion',None,True),('before_record_commit',{'record':'completion.json'},True),
                ('after_record_commit',{'record':'completion.json'},True)]
        for point,detail,committed in points:
            with self.subTest(point=point,detail=detail):
                c=self.case(); original=c.target_snapshot(); self.crash(c.apply,point,detail)
                chain=c.chain()
                if not committed:
                    self.assertIsNone(chain); self.assertEqual(c.target_snapshot(),original)
                    audit_before=tree_snapshot(c.state); audited=c.audit()
                    self.assertEqual(tree_snapshot(c.state),audit_before)
                    self.assertTrue(audited['precommit_staging'][0]['leftover_staging'])
                    old_scratch={p.name:p.read_bytes() for p in c.workspace.glob('.changeset-*.stage')}
                    self.assertEqual(c.apply()['outcome'],'applied_unverified')
                    for name,data in old_scratch.items(): self.assertEqual((c.workspace/name).read_bytes(),data)
                else:
                    self.assertIsNotNone(chain); self.verify_backups(c)
                    before=c.target_snapshot()
                    if chain['completion'] is None:
                        with self.assertRaises(ChangesetError) as exc: c.apply()
                        self.assertEqual(exc.exception.code,'journal_exists')
                        self.assertEqual(c.target_snapshot(),before)
                    audit_before=tree_snapshot(c.state); audited=c.audit()
                    self.assertEqual(tree_snapshot(c.state),audit_before)
                    self.assertTrue(audited['journals'][0]['eligible'])
                    self.assertIn(c.resume(audited)['outcome'],('resumed_unverified','already_completed'))
                self.assertEqual(c.contents(),c.after); self.verify_backups(c)
                done=c.target_snapshot(); self.assertEqual(c.apply()['outcome'],'already_completed')
                self.assertEqual(c.target_snapshot(),done)
    def test_two_process_workspace_lock_covers_disjoint_target_plans(self):
        c=self.case(); original=c.target_snapshot()
        # Build a second authorized plan restricted to another target before a writer starts.
        other=copy.copy(c); other.envelope=copy.deepcopy(c.envelope)
        other.envelope['batch_id']='disjoint-batch'; other.envelope['targets']=[other.envelope['targets'][1]]
        other.prepare(); (other.input/'other-receipt.json').write_bytes(other.receipts)
        (c.input/'receipt.json').write_bytes(c.receipts)
        pid,ready,release=self.start_child(c.apply,'after_prepared_write',hold=True)
        before=tree_snapshot(c.state)
        result=tool.apply(str(c.workspace),str(c.state),str(c.input),other.sha,receipts_locator='other-receipt.json')
        self.assertEqual(result['outcome'],'busy'); self.assertEqual(tree_snapshot(c.state),before)
        audit=c.audit(); self.assertEqual(audit['outcome'],'writer_active'); self.assertNotIn('audit_sha256',audit)
        self.assertEqual(c.target_snapshot(),original)
        os.write(release,b'G')
        self.assertTrue(select.select([ready],[],[],15)[0]); self.assertEqual(os.read(ready,1),b'D')
        self.assertEqual(os.waitstatus_to_exitcode(os.waitpid(pid,0)[1]),0)
        self.assertEqual(c.contents(),c.after)
    def test_partial_recorded_scratch_restage_preserves_old_evidence(self):
        c=self.case(); self.crash(c.apply,'after_prepared_write'); entry=c.chain()['targets'][0]
        old=c.workspace/entry['staged']['name']; old.write_bytes(b'be'); original_inode=old.stat().st_ino
        audit=c.audit(); self.assertTrue(audit['journals'][0]['eligible'])
        self.assertEqual(audit['journals'][0]['targets'][0]['staging']['state'],'partial')
        self.assertEqual(c.resume(audit)['outcome'],'resumed_unverified')
        self.assertEqual(old.read_bytes(),b'be'); self.assertEqual(old.stat().st_ino,original_inode)
        self.assertEqual(c.contents(),c.after)
        self.assertTrue(any(r['kind']=='stage' for r in c.chain()['records']))
        self.assertTrue(c.audit()['journals'][0]['leftover_staging'])
    def test_nonconforming_scratch_and_unrecorded_creation_preserved(self):
        for mutation in ('prefix','mode','foreign','unrecorded'):
            with self.subTest(mutation=mutation):
                c=self.case(); self.crash(c.apply,'after_prepared_write'); entry=c.chain()['targets'][0]
                path=c.workspace/entry['staged']['name']
                if mutation=='prefix': path.write_bytes(b'wrong prefix')
                elif mutation=='mode': path.chmod(0o400)
                elif mutation=='foreign':
                    other=c.workspace/'foreign'; other.write_bytes(b'be'); other.chmod(0o600); os.replace(other,path)
                else:
                    path.write_bytes(b'be'); audit=c.audit()
                    self.crash(lambda:c.resume(audit),'after_stage_create',{'target_index':0})
                before=c.target_snapshot(); scratch={p.name:(p.read_bytes(),p.stat().st_ino,p.stat().st_mode) for p in c.workspace.glob('.changeset-*.stage')}
                audit=c.audit(); self.assertFalse(audit['journals'][0]['eligible'])
                if mutation=='unrecorded': self.assertIn('unrecorded',[e['state'] for e in audit['journals'][0]['leftover_staging']])
                with self.assertRaises(ChangesetError): c.resume(audit)
                self.assertEqual(c.target_snapshot(),before)
                for name,saved in scratch.items():
                    p=c.workspace/name; self.assertEqual((p.read_bytes(),p.stat().st_ino,p.stat().st_mode),saved)
    def test_exact_bytes_on_foreign_before_after_and_noop_inode_refuse(self):
        for name in ('a.txt','b.txt','noop.txt'):
            with self.subTest(name=name):
                c=self.case(); self.crash(c.apply,'after_rename',{'target_index':0})
                path=c.workspace/name; other=c.workspace/'external-replacement'; other.write_bytes(path.read_bytes()); other.chmod(0o600); os.replace(other,path)
                before=c.target_snapshot(); audit=c.audit()
                self.assertFalse(audit['journals'][0]['eligible'])
                with self.assertRaises(ChangesetError): c.resume(audit)
                self.assertEqual(c.target_snapshot(),before)
    def test_changed_evidence_or_freshness_refuses_without_further_writes(self):
        for mutation in ('backup','source','draft','proposed','pin','receipt','metadata','stale','journal'):
            with self.subTest(mutation=mutation):
                c=self.case(conditions=True)
                self.crash(c.apply,'before_rename' if mutation=='journal' else 'after_rename',{'target_index':1 if mutation=='journal' else 0})
                audit=c.audit()
                target=c.plan['targets'][1]
                if mutation in ('backup','source','draft','proposed'):
                    folder,sha={'backup':('backups',target['source']['sha256']),'source':('sources',target['source']['sha256']),
                                'draft':('drafts',target['draft']['sha256']),'proposed':('staged',target['proposed']['sha256'])}[mutation]
                    (c.state/folder/sha).unlink()
                elif mutation=='pin': (c.input/'check.txt').write_bytes(b'changed\n')
                elif mutation=='receipt': (c.input/'receipt.json').write_bytes(b'{}')
                elif mutation=='metadata': (c.workspace/'noop.txt').chmod(0o400)
                elif mutation=='stale':
                    entry=c.chain()['targets'][1]; (c.workspace/entry['staged']['name']).write_bytes(b'de')
                else:
                    record=next((c.state/'journals'/c.sha/'outcomes').glob('*.json'))
                    obj=json.loads(record.read_text()); obj['prev_sha256']='0'*64
                    obj['record_sha256']=digest_object(obj,'record_sha256')
                    record.write_text(canonical(obj))
                before=c.target_snapshot()
                with self.assertRaises(ChangesetError): c.resume(audit)
                self.assertEqual(c.target_snapshot(),before)
    def test_changed_root_identity_refuses_and_blocks_new_apply_same_path(self):
        c=self.case(); self.crash(c.apply,'after_prepared_write'); previous=c.workspace.with_name('previous-workspace')
        c.workspace.rename(previous); c.workspace.mkdir(mode=0o700)
        for name,data in c.before.items(): (c.workspace/name).write_bytes(data); (c.workspace/name).chmod(0o600)
        original=c.target_snapshot(); audit=c.audit()
        # A fresh inode has no existing lock yet; it must not receive a reusable audit digest.
        self.assertFalse(audit['reusable_digest'])
        with self.assertRaises(ChangesetError): c.apply()
        c.envelope['batch_id']='new-root-at-old-path'; c.prepare()
        with self.assertRaises(ChangesetError) as exc: c.apply()
        self.assertEqual(exc.exception.code,'journal_exists'); self.assertEqual(c.target_snapshot(),original)
    def test_unresolved_same_inode_at_new_path_also_blocks_new_apply(self):
        c=self.case(); self.crash(c.apply,'after_prepared_write')
        moved=c.workspace.with_name('moved-workspace'); c.workspace.rename(moved); c.workspace=moved
        original=c.target_snapshot(); c.envelope['batch_id']='same-inode-new-path'; c.prepare()
        with self.assertRaises(ChangesetError) as exc: c.apply()
        self.assertEqual(exc.exception.code,'journal_exists'); self.assertEqual(c.target_snapshot(),original)
    def test_rehashed_boolean_completion_and_intent_indices_refuse(self):
        c=self.case(); c.apply(); completion=c.state/'journals'/c.sha/'completion.json'
        obj=json.loads(completion.read_text()); obj['targets'][0]['target_index']=False
        obj['completion_sha256']=digest_object(obj,'completion_sha256'); completion.write_text(canonical(obj))
        before=c.target_snapshot()
        with self.assertRaises(ChangesetError): c.apply()
        self.assertEqual(c.target_snapshot(),before)
        c=self.case(); self.crash(c.apply,'after_prepared_write'); stage=c.chain()['targets'][0]['staged']
        intent=c.state/'journals'/c.sha/'scratch'/(stage['intent_id']+'.intent.json')
        obj=json.loads(intent.read_text()); obj['target_index']=False; obj['sha256']=digest_object(obj,'sha256'); intent.write_text(canonical(obj))
        before=c.target_snapshot(); audited=c.audit(); self.assertFalse(audited['journals'][0]['eligible'])
        with self.assertRaises(ChangesetError): c.resume(audited)
        self.assertEqual(c.target_snapshot(),before)
    def test_rehashed_float_prepared_outcome_and_completion_records_refuse(self):
        for kind in ('prepared','outcome','completion'):
            with self.subTest(kind=kind):
                c=self.case()
                if kind=='completion': c.apply()
                else: self.crash(c.apply,'after_prepared_write' if kind=='prepared' else 'before_completion')
                if kind=='prepared': path=c.state/'journals'/c.sha/'prepared.json'; field='prepared_sha256'
                elif kind=='completion': path=c.state/'journals'/c.sha/'completion.json'; field='completion_sha256'
                else: path=next((c.state/'journals'/c.sha/'outcomes').glob('*.json')); field='record_sha256'
                obj=json.loads(path.read_text())
                if kind=='prepared': obj['targets'][0]['staged']['inode']=float(obj['targets'][0]['staged']['inode'])
                elif kind=='completion': obj['targets'][0]['target_index']=0.0
                else: obj['sequence']=float(obj['sequence'])
                # An independent permissive encoder creates the malformed
                # rehashed record; production canonical rejects these floats.
                raw=json.dumps({k:v for k,v in obj.items() if k!=field},sort_keys=True,separators=(',',':'),ensure_ascii=True)
                obj[field]=hashlib.sha256(raw.encode()).hexdigest()
                path.write_text(json.dumps(obj,sort_keys=True,separators=(',',':'),ensure_ascii=True))
                before=tree_snapshot(c.base)
                for command in ('audit','apply','resume'):
                    self.assert_cli_refusal(c,command,'float_in_structural_record')
                    self.assertEqual(tree_snapshot(c.base),before)
    def test_foreign_journal_entry_and_missing_precommit_plan_refuse_readonly(self):
        for kind in ('foreign_entry','missing_plan'):
            with self.subTest(kind=kind):
                c=self.case()
                self.crash(c.apply,'after_prepared_write' if kind=='foreign_entry' else 'after_stage_create')
                if kind=='foreign_entry': (c.state/'journals'/'notes.txt').write_bytes(b'synthetic foreign entry')
                else: (c.state/'plans'/(c.sha+'.json')).unlink()
                expected='unknown_state_entry' if kind=='foreign_entry' else 'plan_missing'
                before=tree_snapshot(c.base)
                for command in ('audit','apply','resume'):
                    self.assert_cli_refusal(c,command,expected)
                    self.assertEqual(tree_snapshot(c.base),before)
    def test_unsafe_lock_paths_refuse_without_state_or_target_writes(self):
        for kind in ('symlink','hardlink','mode','fifo','directory','foreign_owner'):
            with self.subTest(kind=kind):
                c=self.case(); st=c.workspace.stat()
                path=c.state/'locks'/(journal.workspace_key(st.st_dev,st.st_ino)+'.lock')
                if kind=='symlink': path.symlink_to(c.input/'a.txt.json')
                elif kind=='hardlink':
                    other=c.state/'foreign-lock'; other.write_bytes(b''); other.chmod(0o600); os.link(other,path)
                elif kind=='fifo': os.mkfifo(path,0o600)
                elif kind=='directory': path.mkdir(mode=0o700)
                else: path.write_bytes(b''); path.chmod(0o644 if kind=='mode' else 0o600)
                native_fstat=os.fstat; lock_stat=path.lstat()
                def foreign_owner(fd):
                    current=native_fstat(fd)
                    if kind=='foreign_owner' and (current.st_dev,current.st_ino)==(lock_stat.st_dev,lock_stat.st_ino):
                        # Foreign ownership is modeled only for this exact
                        # lock descriptor; no privileged chown is claimed.
                        return SimpleNamespace(st_mode=current.st_mode,st_nlink=current.st_nlink,st_uid=os.geteuid()+1)
                    return current
                expected=('lock_unsafe','filesystem_unavailable') if kind in ('symlink','directory') else 'lock_unsafe'
                before=tree_snapshot(c.base)
                with patch.object(fs.os,'fstat',foreign_owner):
                    for command in ('audit','apply','resume'):
                        self.assert_cli_refusal(c,command,expected)
                        self.assertEqual(tree_snapshot(c.base),before)
    def test_resume_refuses_another_unresolved_prepared_journal(self):
        c=self.case(); other=copy.copy(c); other.envelope=copy.deepcopy(c.envelope)
        other.envelope['batch_id']='conflicting-synthetic-batch'; other.prepare()
        (c.input/'receipt.json').write_bytes(c.receipts)
        self.crash(c.apply,'after_prepared_write'); audit=c.audit()
        prepared=json.loads((c.state/'journals'/c.sha/'prepared.json').read_text())
        prepared['plan_sha256']=other.sha; prepared['batch_id']=other.plan['batch_id']
        prepared['prepared_sha256']=digest_object(prepared,'prepared_sha256')
        directory=c.state/'journals'/other.sha; directory.mkdir(mode=0o700)
        path=directory/'prepared.json'; path.write_text(canonical(prepared)); path.chmod(0o600)
        # Deliberately inject conflicting state that cooperating writers cannot
        # create. Both prepared journals must parse: a second unprepared plan
        # only tests journal_missing, before resume's workspace-wide guard.
        self.assertIsNone(c.chain()['completion']); self.assertIsNone(other.chain()['completion'])
        before=tree_snapshot(c.base)
        with self.assertRaises(ChangesetError) as exc: c.resume(audit)
        self.assertEqual(exc.exception.code,'journal_exists')
        self.assertEqual(tree_snapshot(c.base),before)
    def test_receipt_changed_after_prepare_is_caught_before_first_rename(self):
        c=self.case(conditions=True); original=c.target_snapshot()
        def hook(point,detail):
            if point=='after_prepared_write': (c.input/'receipt.json').write_bytes(b'{}')
        faults.set_hook(hook); self.addCleanup(faults.clear_hook)
        with self.assertRaises(ChangesetError): c.apply()
        self.assertEqual(c.target_snapshot(),original)

if __name__=='__main__': unittest.main()
