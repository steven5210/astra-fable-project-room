import copy
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import changeset_tool as tool
import changeset_fs as fs
import changeset_journal as journal
from changeset_core import ChangesetError,sha256

class Case:
    """Small synthetic operator fixture; expected output literals are independent."""
    def __init__(self,base,conditions=False,inline=False):
        self.base=Path(base)
        self.workspace=self.base/'workspace'; self.state=self.base/'state'; self.input=self.base/'input'
        for path in (self.workspace,self.state,self.input): path.mkdir(mode=0o700)
        self.before={'a.txt':b'alpha\n','b.txt':b'bravo\n','noop.txt':b'steady\n'}
        self.after={'a.txt':b'beta\n','b.txt':b'delta\n','noop.txt':b'steady\n'}
        targets=[]; self.drafts={}
        for idx,(name,data) in enumerate(self.before.items()):
            path=self.workspace/name; path.write_bytes(data); path.chmod(0o600)
            operations=[] if name=='noop.txt' else [{'id':'op-'+str(idx),'anchor':data.decode().strip(),'replacement':self.after[name].decode().strip()}]
            draft=json.dumps({'changeset_format':'anchored-text-v1','document':name,'request_id':'request-'+str(idx),'operations':operations}).encode()
            locator=name+'.json'; self.drafts[locator]=draft
            if not inline: (self.input/locator).write_bytes(draft)
            targets.append({'target':name,'source':{'bytes':len(data),'sha256':sha256(data),'mode':0o600},
                            'draft':{'locator':locator,'bytes':len(draft),'sha256':sha256(draft)},'renderer':None})
        checks=[]
        if conditions:
            (self.input/'check.txt').write_bytes(b'check input\n')
            checks=[{'id':'check-1','phase':'pre_apply','text':'Synthetic input is exact','description':'Reference check in the test',
                     'inputs':[{'id':'pin-1','root':'input','path':'check.txt','bytes':12,'sha256':sha256(b'check input\n')}]}]
        self.envelope={'envelope_version':1,'batch_id':'batch-one','execution':'sequential-v1','targets':targets,'conditions':checks}
        self.inline=inline; self.sha=None
    def prepare(self):
        self.planned=tool.plan(str(self.workspace),str(self.state),str(self.input),json.dumps(self.envelope).encode(),self.drafts if self.inline else None)
        self.sha=self.planned['plan_sha256']; self.plan=json.loads((self.state/'plans'/(self.sha+'.json')).read_text())
        template=json.loads((self.state/'templates'/(self.sha+'.json')).read_text())
        assert template['receipt_version']==1
        # Synthetic reference check executed separately from the operator tool.
        for item in template['conditions']:
            assert (self.input/'check.txt').read_bytes()==b'check input\n'
            item['result']='pass'; item['evidence']='Synthetic reference assertion passed'
        self.receipts=json.dumps(template).encode(); (self.input/'receipt.json').write_bytes(self.receipts)
        return self.sha
    def apply(self): return tool.apply(str(self.workspace),str(self.state),str(self.input),self.sha,receipts_locator='receipt.json')
    def audit(self): return tool.audit(str(self.workspace),str(self.state))
    def resume(self,audit=None):
        audit=self.audit() if audit is None else audit
        return tool.resume(str(self.workspace),str(self.state),str(self.input),self.sha,audit['audit_sha256'],receipts_locator='receipt.json')
    def target_snapshot(self):
        return {name:(path.read_bytes(),path.stat().st_dev,path.stat().st_ino,path.stat().st_mode) for name in self.before for path in [self.workspace/name]}
    def contents(self): return {name:(self.workspace/name).read_bytes() for name in self.before}
    def chain(self):
        state=fs.open_root(str(self.state),owner_only=True)
        try: return journal.read_chain(state,self.sha)
        finally: state.close()

def tree_snapshot(path):
    """Only synthetic fixtures; never follow links or open FIFOs in the oracle."""
    result={}
    for p in path.rglob('*'):
        st=p.lstat()
        data=p.read_bytes() if stat.S_ISREG(st.st_mode) else os.readlink(p) if stat.S_ISLNK(st.st_mode) else None
        result[str(p.relative_to(path))]=(st.st_dev,st.st_ino,st.st_mode,st.st_nlink,data)
    return result

class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='changeset-workflow-',dir='/private/tmp' if os.path.isdir('/private/tmp') else None)
        self.addCleanup(self.temp.cleanup)
        self.case=Case(self.temp.name)
    def prepare(self,case=None):
        case=case or self.case
        try: case.prepare()
        except ChangesetError as exc:
            if exc.code=='unsupported_capability': self.skipTest('Native Linux trusted-xattr capability unavailable; no success claimed')
            raise
        return case
    def test_native_plan_apply_audit_and_verified_replay(self):
        c=self.case; original=tree_snapshot(c.workspace); self.prepare()
        self.assertEqual(tree_snapshot(c.workspace),original)
        for target in c.plan['targets']:
            self.assertEqual((c.state/'staged'/target['proposed']['sha256']).read_bytes(),c.after[target['target']])
        self.assertEqual(c.apply()['outcome'],'applied_unverified'); self.assertEqual(c.contents(),c.after)
        before=tree_snapshot(c.state); targets=c.target_snapshot(); audit=c.audit()
        self.assertTrue(audit['reusable_digest']); self.assertEqual(tree_snapshot(c.state),before)
        self.assertEqual(c.apply()['outcome'],'already_completed'); self.assertEqual(c.target_snapshot(),targets)
        self.assertEqual(c.resume()['outcome'],'already_completed'); self.assertEqual(c.target_snapshot(),targets)
    def test_completed_receipt_does_not_attest_modified_targets(self):
        c=self.prepare(); c.apply(); (c.workspace/'b.txt').write_bytes(b'unknown edit\n'); before=c.target_snapshot()
        with self.assertRaises(ChangesetError): c.apply()
        self.assertEqual(c.target_snapshot(),before)
    def test_intentional_new_batch_after_revert(self):
        c=self.prepare(); first=c.sha; c.apply()
        for name,data in c.before.items(): (c.workspace/name).write_bytes(data)
        c.envelope['batch_id']='independent-new-batch'; c.prepare()
        self.assertNotEqual(c.sha,first); self.assertEqual(c.apply()['outcome'],'applied_unverified')
        self.assertEqual(c.contents(),c.after)
    def test_audit_empty_state_does_not_initialize_it(self):
        c=self.case; before=tree_snapshot(c.state)
        result=c.audit(); self.assertFalse(result['reusable_digest']); self.assertNotIn('audit_sha256',result)
        self.assertEqual(tree_snapshot(c.state),before)
    def test_nested_state_refuses_before_candidate_write(self):
        c=self.case; nested=c.workspace/'state'; nested.mkdir(mode=0o700); before=tree_snapshot(c.workspace)
        with self.assertRaises(ChangesetError): tool.plan(str(c.workspace),str(nested),str(c.input),json.dumps(c.envelope).encode())
        self.assertEqual(tree_snapshot(c.workspace),before)
    def test_input_draft_missing_refuses_but_inline_is_retained(self):
        c=self.prepare(); (c.input/'b.txt.json').unlink(); before=c.target_snapshot()
        with self.assertRaises(ChangesetError): c.apply()
        self.assertEqual(c.target_snapshot(),before)
        base=Path(self.temp.name)/'inline'; base.mkdir(); other=Case(base,inline=True); self.prepare(other)
        self.assertEqual(other.apply()['outcome'],'applied_unverified'); self.assertEqual(other.contents(),other.after)
    def test_late_foreign_inode_mode_and_staged_corruption_refuse(self):
        c=self.prepare()
        for mutation in ('foreign','mode','staged'):
            with self.subTest(mutation=mutation):
                base=Path(self.temp.name)/mutation; base.mkdir(); other=Case(base); self.prepare(other)
                if mutation=='foreign':
                    replacement=other.workspace/'replacement'; replacement.write_bytes(other.before['noop.txt']); replacement.chmod(0o600)
                    os.replace(replacement,other.workspace/'noop.txt')
                elif mutation=='mode': (other.workspace/'noop.txt').chmod(0o400)
                else: (other.state/'staged'/other.plan['targets'][-1]['proposed']['sha256']).write_bytes(b'bad')
                before=other.target_snapshot()
                with self.assertRaises(ChangesetError): other.apply()
                self.assertEqual(other.target_snapshot(),before)
    def test_last_metadata_stage_failure_leaves_all_targets_original(self):
        c=self.prepare(); before=c.target_snapshot(); native=journal.apply_meta; calls=[]
        def fail_second(fd,meta):
            calls.append(1)
            if len(calls)==2: raise ChangesetError('synthetic_metadata_failure')
            return native(fd,meta)
        with patch.object(journal,'apply_meta',fail_second):
            with self.assertRaises(ChangesetError): c.apply()
        self.assertEqual(c.target_snapshot(),before); self.assertIsNone(c.chain())
    def test_pinned_input_receipt_and_root_mode_refusals(self):
        for mutation in ('pin','receipt','root'):
            with self.subTest(mutation=mutation):
                base=Path(self.temp.name)/mutation; base.mkdir(); c=Case(base,conditions=True); self.prepare(c)
                if mutation=='pin': (c.input/'check.txt').write_bytes(b'wrong input\n')
                elif mutation=='receipt':
                    receipt=json.loads(c.receipts); receipt['conditions'][0]['result']='unknown'; (c.input/'receipt.json').write_text(json.dumps(receipt))
                else: c.workspace.chmod(0o750)
                before=c.target_snapshot()
                with self.assertRaises(ChangesetError): c.apply()
                self.assertEqual(c.target_snapshot(),before)

if __name__=='__main__': unittest.main()
