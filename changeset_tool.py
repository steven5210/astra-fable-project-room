"""Explicit operator CLI and importable changeset interfaces."""
import argparse
import os
import sys
from changeset_core import *
from changeset_fs import *
from changeset_plan import build_plan, plan_summary
from changeset_journal import apply_plan, audit_workspace, resume_plan, open_roots, validate_roots

VERSION='changeset-tool-v1'

def _public(result):
    return json.dumps(result,sort_keys=True,separators=(',',':'),ensure_ascii=True)+LF

def plan(workspace_path,state_root_path,input_path,envelope,supplied_drafts=None):
    state,workspace,input_root=open_roots(workspace_path,state_root_path,input_path)
    try: return plan_summary(build_plan(workspace,state,input_root,envelope,supplied_drafts),state)
    finally:
        workspace.close(); input_root.close(); state.close()

def apply(workspace_path,state_root_path,input_path,plan_sha256,receipts=None,receipts_locator=None):
    state,workspace,input_root=open_roots(workspace_path,state_root_path,input_path,initialize=False)
    try: return apply_plan(workspace,state,input_root,plan_sha256,receipts,receipts_locator)
    finally:
        workspace.close(); input_root.close(); state.close()

def audit(workspace_path,state_root_path):
    state=workspace=None
    try:
        workspace=open_root(workspace_path,owned=False)
        state=open_root(state_root_path,owner_only=True)
        validate_roots(workspace,state)
        return audit_workspace(workspace,state)
    finally:
        if workspace: workspace.close()
        if state: state.close()

def resume(workspace_path,state_root_path,input_path,plan_sha256,audit_sha256,receipts=None,receipts_locator=None):
    state,workspace,input_root=open_roots(workspace_path,state_root_path,input_path,initialize=False)
    try: return resume_plan(workspace,state,input_root,plan_sha256,audit_sha256,receipts,receipts_locator)
    finally:
        workspace.close(); input_root.close(); state.close()

class _Parser(argparse.ArgumentParser):
    def error(self,message):
        raise ChangesetError('invalid_cli')

def _add_base(parser):
    parser.add_argument('--workspace',required=True)
    parser.add_argument('--state-root',required=True)

def _read_envelope(input_dir,locator):
    root=open_root(input_dir,owner_only=True)
    try:
        parts=normalize_rel(locator); fd=descend(root,parts[:-1])
        try: return read_file_at(fd,parts[-1])[0]
        finally: os.close(fd)
    finally: root.close()

def main(argv=None):
    parser=_Parser(prog='changeset_tool.py')
    sub=parser.add_subparsers(dest='command',required=True,parser_class=_Parser)
    for name in ('plan','apply','audit','resume'):
        p=sub.add_parser(name); _add_base(p)
        if name!='audit': p.add_argument('--input-dir',required=True)
        if name=='plan': p.add_argument('--envelope',required=True)
        if name in ('apply','resume'):
            p.add_argument('--plan-sha256',required=True); p.add_argument('--receipts',required=True)
        if name=='resume': p.add_argument('--audit-sha256',required=True)
    try:
        args=parser.parse_args(argv)
        if args.command=='plan': result=plan(args.workspace,args.state_root,args.input_dir,_read_envelope(args.input_dir,args.envelope))
        elif args.command=='apply': result=apply(args.workspace,args.state_root,args.input_dir,args.plan_sha256,receipts_locator=args.receipts)
        elif args.command=='audit': result=audit(args.workspace,args.state_root)
        else: result=resume(args.workspace,args.state_root,args.input_dir,args.plan_sha256,args.audit_sha256,receipts_locator=args.receipts)
        sys.stdout.write(_public(result)); return 0
    except ChangesetError as exc:
        sys.stdout.write(_public({'tool':'changeset_tool','version':VERSION,'outcome':'refused','error':{'code':exc.code}})); return 2
    except OSError:
        sys.stdout.write(_public({'tool':'changeset_tool','version':VERSION,'outcome':'refused','error':{'code':'filesystem_unavailable'}})); return 2
    except Exception:
        sys.stdout.write(_public({'tool':'changeset_tool','version':VERSION,'outcome':'refused','error':{'code':'internal_error'}})); return 2

if __name__=='__main__': sys.exit(main())
