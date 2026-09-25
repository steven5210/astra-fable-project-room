"""CLI smoke may launch only the Python executable against synthetic inputs."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from test_changeset_workflow import Case,tree_snapshot

class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='changeset-cli-',dir='/private/tmp' if os.path.isdir('/private/tmp') else None)
        self.addCleanup(self.temp.cleanup); self.case=Case(self.temp.name,conditions=True)
        (self.case.input/'envelope.json').write_text(json.dumps(self.case.envelope))
    def run_cli(self,command,extra=(),expected=0):
        c=self.case
        args=[sys.executable,'changeset_tool.py',command,'--workspace',str(c.workspace),'--state-root',str(c.state)]
        if command!='audit': args+=['--input-dir',str(c.input)]
        env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',CHANGESET_FAULT='after_prepared_write',CHANGESET_TEST_HOOK='exit')
        run=subprocess.run(args+list(extra),cwd=Path(__file__).resolve().parent,env=env,capture_output=True,text=True,timeout=30)
        self.assertEqual(run.stderr,''); self.assertIn(run.returncode,expected if isinstance(expected,tuple) else (expected,))
        self.assertEqual(len(run.stdout.splitlines()),1); result=json.loads(run.stdout)
        for private in (str(c.base),'alpha','bravo','steady','Synthetic input','check-1','pin-1'):
            self.assertNotIn(private,run.stdout)
        return result
    def prepare(self):
        c=self.case; result=self.run_cli('plan',['--envelope','envelope.json'],expected=(0,2))
        if result.get('error',{}).get('code')=='unsupported_capability': self.skipTest('Native trusted-xattr support unavailable')
        c.sha=result['plan_sha256']; template=json.loads((c.state/'templates'/(c.sha+'.json')).read_text())
        self.assertEqual((c.input/'check.txt').read_bytes(),b'check input\n')
        for item in template['conditions']: item['result']='pass'; item['evidence']='Test-local reference input comparison'
        (c.input/'receipt.json').write_text(json.dumps(template)); return result
    def test_cli_plan_apply_audit_and_replay_smoke_fault_env_is_inert(self):
        c=self.case; original=tree_snapshot(c.workspace); self.prepare()
        self.assertEqual(tree_snapshot(c.workspace),original)
        args=['--plan-sha256',c.sha,'--receipts','receipt.json']
        self.assertEqual(self.run_cli('apply',args)['outcome'],'applied_unverified'); self.assertEqual(c.contents(),c.after)
        before=tree_snapshot(c.state); targets=c.target_snapshot(); audited=self.run_cli('audit')
        self.assertEqual(tree_snapshot(c.state),before)
        self.assertEqual(self.run_cli('resume',args+['--audit-sha256',audited['audit_sha256']])['outcome'],'already_completed')
        self.assertEqual(c.target_snapshot(),targets)
    def test_invalid_envelope_missing_files_and_cli_errors_are_safe_json(self):
        c=self.case; original=tree_snapshot(c.workspace); (c.input/'invalid.json').write_bytes(b'{"secret invalid text":')
        for extra in (['--envelope','invalid.json'],['--envelope','not-present.json'],[],['--envelope','../outside']):
            with self.subTest(extra=extra):
                result=self.run_cli('plan',extra,expected=2); self.assertEqual(result['outcome'],'refused')
                self.assertNotEqual(result['error']['code'],'internal_error')
        self.assertEqual(tree_snapshot(c.workspace),original)
    def test_unchanged_nonempty_receipt_template_refuses_before_writes(self):
        c=self.case; self.prepare(); template=(c.state/'templates'/(c.sha+'.json')).read_bytes()
        (c.input/'receipt.json').write_bytes(template); original=c.target_snapshot()
        result=self.run_cli('apply',['--plan-sha256',c.sha,'--receipts','receipt.json'],expected=2)
        self.assertEqual(result['error']['code'],'receipt_not_passing'); self.assertEqual(c.target_snapshot(),original)

if __name__=='__main__': unittest.main()
