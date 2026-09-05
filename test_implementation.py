"""Implementation tests use temporary git repositories and fake Claude processes."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest
import uuid

import implementation as impl
import session_paths
import test_room


FAKE = r'''#!/usr/bin/env python3
import datetime, json, os, pathlib, sys, time
root = pathlib.Path(__file__).parent
mode = (root / "implementation-mode.txt").read_text().strip()
argv = sys.argv[1:]
prompt = sys.stdin.read()
packet = json.loads(prompt.split("IMPLEMENTATION PACKET (JSON):\n", 1)[1])
session = argv[argv.index("--resume") + 1] if "--resume" in argv else argv[argv.index("--session-id") + 1]
with (root / "implementation-calls.jsonl").open("a") as log:
    log.write(json.dumps({"argv":argv,"session":session,"cwd":os.getcwd(),"packet":packet,"pid":os.getpid(),"config_dir_override":os.environ.get("CLAUDE_CONFIG_DIR")})+"\n")
if mode == "timeout":
    print("partial", flush=True)
    time.sleep(60)
if mode != "scope":
    pathlib.Path("feature.txt").write_text("implemented\n")
if mode == "malformed":
    print("bad output")
    sys.exit(0)
report = {
 "summary":"Implemented the agreed fixture feature.", "spec_revision":packet["spec_revision"],
 "spec_sha256":packet["spec_sha256"], "baseline_commit":packet["baseline_commit"],
 "implementation_complete": mode != "scope", "outcome":"scope_change" if mode == "scope" else "completed",
 "scope_change":"The requested behavior needs a product decision." if mode == "scope" else "", "backlog":[],
 "routing_log":[{"task":"fixture change","tier":"fable","requested_model":"claude-fable-5-1","actual_model":"claude-fable-5-1",
 "reason":"Bounded fixture work","result":"implemented","fixes":[],"escalation":"none"}],
 "changes":["feature.txt"], "tests_reported":["Self-reported evidence is not independent"],
 "review_findings":[], "remaining_gaps":[]}
if mode == "wrong-hash": report["spec_sha256"] = "0" * 64
result={"type":"result","subtype":"success","is_error":False,"session_id":session,
 "modelUsage":{"claude-fable-5-1":{},"helper-model":{}},"structured_output":report}
if mode == "denied": result["permission_denials"]=[{"tool_name":"Bash","tool_input":"denied fixture operation"}]
if mode == "wrong-session": result["session_id"]="wrong"
if mode == "gaps": report["remaining_gaps"]=["Missing required independent engineering review"]
event={"type":"assistant","sessionId":session,"timestamp":datetime.datetime.now(datetime.timezone.utc).isoformat(),
 "message":{"id":"fixture-message","model":"helper-model" if mode=="wrong-primary" else "claude-fable-5-1",
 "content":[{"type":"thinking","thinking":"DO_NOT_EXPOSE_PRIVATE_THINKING"},
 {"type":"tool_use","name":"StructuredOutput","id":"tool-fixture","input":report}]}}
if mode != "missing-evidence":
    with (root / (session + ".jsonl")).open("a") as out:
        out.write(json.dumps(event)+"\n")
print(json.dumps(result))
if mode == "nonzero": sys.exit(4)
'''


class ImplementationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_room.RoomTests(methodName="test_restart_resumes_same_session_and_exact_model")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root, self.room = self.fixture.root, self.fixture.room
        self.fixture.ask()
        self.fixture.approve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "core.hooksPath", "/dev/null")
        (self.project / "baseline.txt").write_text("baseline\n")
        (self.project / ".gitignore").write_text("cache/\n__pycache__/\n")
        self.git("add", ".")
        self.git("-c", "commit.gpgSign=false", "commit", "-q", "-m", "fixture baseline")
        self.fake = self.root / "fake-implementation"
        self.fake.write_text(FAKE.replace("#!/usr/bin/env python3", f"#!{sys.executable}", 1))
        self.fake.chmod(0o700)
        self.mode = self.root / "implementation-mode.txt"
        self.mode.write_text("normal")
        self.mcp = self.root / "impl-mcp.json"
        self.mcp.write_text('{"mcpServers":{}}')
        self.config = self.root / "implementation-config.json"
        self.config.write_text(json.dumps({"claude_bin":str(self.fake),"model":"claude-fable-5-1",
            "expected_model_ids":["claude-fable-5-1"],"timeout_seconds":5,"gate_timeout_seconds":5,
            "claude_config_dir_override":None,
            "session_transcript_path":str(self.root / "{session_id}.jsonl"),
            "extra_args":["--effort","max","--strict-mcp-config","--mcp-config",str(self.mcp),
                          "--setting-sources","","--settings",'{"disableAllHooks":true}',
                          "--permission-prompts","none","--permission-mode","auto"]}))
        self.gates = [[sys.executable,"-c","from pathlib import Path; assert Path('feature.txt').read_text() == 'implemented\\n'"]]

    def git(self, *args):
        result = subprocess.run(["git","-C",str(self.project),*args],capture_output=True,check=False)
        self.assertEqual(result.returncode,0,result.stderr.decode())
        return result.stdout

    def prepare(self, gates=None, authorization="Implement the agreed fixture feature in an isolated worktree."):
        return impl.prepare_handoff(self.room,self.project,1,authorization,gates or self.gates,self.config)

    def calls(self):
        path = self.root / "implementation-calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_authorization_agreement_clean_base_and_idempotent_isolation(self):
        with self.assertRaises(impl.ImplementationError):
            impl.prepare_handoff(self.room,self.project,1,"",self.gates,self.config)
        (self.project / "uncommitted.txt").write_text("keep me")
        with self.assertRaisesRegex(impl.ImplementationError,"clean"):
            self.prepare()
        (self.project / "uncommitted.txt").unlink()
        handoff = self.prepare()
        again = self.prepare()
        self.assertEqual(handoff["handoff_id"],again["handoff_id"])
        self.assertEqual(handoff["baseline_commit"],self.git("rev-parse","HEAD").decode().strip())
        self.assertNotEqual(Path(handoff["worktree_path"]),self.project)
        self.assertFalse((self.project / "feature.txt").exists())
        self.assertEqual(len(self.calls()),0)
        with self.assertRaises(impl.ImplementationError):
            impl.prepare_handoff(self.room,self.project,2,"Implement revision two",self.gates,self.config)

    def test_real_independent_gates_and_astra_acceptance_are_required(self):
        handoff=self.prepare()
        result=impl.run_implementation(handoff["handoff_path"])
        self.assertEqual(result["phase"],"awaiting_astra_review",result.get("error"))
        self.assertTrue(result["gates_passed"])
        self.assertFalse(result["astra_accepted"])
        self.assertNotIn("entries",result["candidate"])
        self.assertIn("path_count",result["candidate"])
        saved=json.loads(Path(result["state_path"]).read_text())
        self.assertIn("entries",saved["candidate"])
        self.assertEqual(result["primary_model"],"claude-fable-5-1")
        self.assertEqual(result["auxiliary_models"],["helper-model"])
        self.assertNotIn("DO_NOT_EXPOSE_PRIVATE_THINKING",json.dumps(result))
        with self.assertRaises(impl.ImplementationError):
            impl.record_astra_review(handoff["handoff_path"],True,"")
        accepted=impl.record_astra_review(handoff["handoff_path"],True,"Checked expected feature behavior and independent gate evidence.")
        self.assertEqual(accepted["phase"],"accepted")
        self.assertFalse((self.project / "feature.txt").exists())
        self.assertEqual(impl.run_implementation(handoff["handoff_path"])["phase"],"accepted")
        self.assertEqual(len(self.calls()),1)

    def test_failed_gate_cannot_accept_and_explicit_correction_resumes_same_session(self):
        gate=[sys.executable,"-c","from pathlib import Path; import sys; sys.exit(0 if Path('allow.txt').exists() else 1)"]
        handoff=self.prepare(gates=[gate])
        result=impl.run_implementation(handoff["handoff_path"])
        self.assertFalse(result["gates_passed"])
        with self.assertRaisesRegex(impl.ImplementationError,"failed gates"):
            impl.record_astra_review(handoff["handoff_path"],True,"Review is present but gates failed.")
        impl.run_implementation(handoff["handoff_path"])
        self.assertEqual(len(self.calls()),1)
        correction=impl.request_changes(handoff["handoff_path"],"Diagnosed missing allow.txt: add it and rerun the original gate.")
        self.assertEqual(correction["phase"],"correction_pending")
        # Fake implementation receives the correction and creates the missing file.
        self.fake.write_text(self.fake.read_text().replace('if mode != "scope":', 'if "correction_request" in packet:\n    pathlib.Path("allow.txt").write_text("allowed")\nif mode != "scope":'))
        fixed=impl.run_implementation(handoff["handoff_path"])
        self.assertTrue(fixed["gates_passed"],fixed.get("error"))
        self.assertEqual(fixed["implementation_session_id"],handoff["implementation_session_id"])
        self.assertIn("--resume",self.calls()[1]["argv"])
        self.assertIn("previous_gate_output",self.calls()[1]["packet"])
        self.assertEqual(fixed["attempt_count"],2)

    def test_untracked_modes_symlinks_and_post_gate_changes_are_bound(self):
        handoff=self.prepare()
        impl.run_implementation(handoff["handoff_path"])
        worktree=Path(handoff["worktree_path"])
        original=impl.candidate_snapshot(worktree)
        (worktree / "feature.txt").chmod(0o755)
        self.assertNotEqual(impl.candidate_snapshot(worktree)["sha256"],original["sha256"])
        with self.assertRaisesRegex(impl.ImplementationError,"changed after"):
            impl.record_astra_review(handoff["handoff_path"],True,"Inspected product behavior.")
        (worktree / "feature.txt").chmod(0o644)
        (worktree / "extra.txt").write_text("new untracked source")
        self.assertNotEqual(impl.candidate_snapshot(worktree)["sha256"],original["sha256"])
        (worktree / "extra.txt").unlink()
        (worktree / "link").symlink_to("baseline.txt")
        linked=impl.candidate_snapshot(worktree)
        self.assertEqual(next(item for item in linked["entries"] if item["path"]=="link")["kind"],"symlink")

    def test_ignored_gate_output_is_allowed_but_candidate_mutations_block(self):
        gate=[sys.executable,"-c","from pathlib import Path; Path('cache').mkdir(exist_ok=True); Path('cache/output').write_text('generated')"]
        handoff=self.prepare(gates=[gate])
        result=impl.run_implementation(handoff["handoff_path"])
        self.assertTrue(result["gates_passed"],result.get("error"))
        other=self.prepare(gates=[[sys.executable,"-c","from pathlib import Path; Path('feature.txt').write_text('gate changed source')"]],authorization="Separate authorized gate mutation fixture")
        blocked=impl.run_implementation(other["handoff_path"])
        self.assertEqual(blocked["phase"],"blocked")
        self.assertIn("gate changed",blocked["error"])

    def test_wrong_identity_terminal_or_missing_evidence_blocks_without_replay(self):
        for mode in ("wrong-primary","wrong-session","wrong-hash","missing-evidence","malformed","nonzero","denied"):
            with self.subTest(mode=mode):
                self.mode.write_text(mode)
                handoff=self.prepare(authorization="Authorized isolated fixture "+mode)
                result=impl.run_implementation(handoff["handoff_path"])
                self.assertEqual(result["phase"],"blocked")
                count=len(self.calls())
                impl.run_implementation(handoff["handoff_path"])
                self.assertEqual(len(self.calls()),count)
                with self.assertRaises(impl.ImplementationError):
                    impl.request_changes(handoff["handoff_path"],"Try again")

    def test_scope_changes_return_to_astra_without_running_gates(self):
        self.mode.write_text("scope")
        handoff=self.prepare()
        result=impl.run_implementation(handoff["handoff_path"])
        self.assertEqual(result["phase"],"scope_change",result.get("error"))
        self.assertEqual(result["gate_results"],[])
        self.assertFalse(result["astra_accepted"])
        with self.assertRaises(impl.ImplementationError):
            impl.request_changes(handoff["handoff_path"],"Broaden scope silently")

    def test_owned_config_pins_inputs_and_rejects_tampered_handoff(self):
        handoff=self.prepare()
        self.mcp.write_text('{"mcpServers":{"changed":{}}}')
        result=impl.run_implementation(handoff["handoff_path"])
        self.assertEqual(result["phase"],"awaiting_astra_review",result.get("error"))
        pin=Path(handoff["handoff_path"]).parent / "spec.md"
        pin.write_text("tampered")
        with self.assertRaisesRegex(impl.ImplementationError,"modified"):
            impl.implementation_status(handoff["handoff_path"])

    def test_tampered_gate_evidence_cannot_be_approved_or_used_for_correction(self):
        handoff=self.prepare()
        result=impl.run_implementation(handoff["handoff_path"])
        Path(result["gate_results"][0]["stdout_path"]).write_text("invented success")
        with self.assertRaisesRegex(impl.ImplementationError,"gate output changed"):
            impl.record_astra_review(handoff["handoff_path"],True,"Independent review text.")
        with self.assertRaisesRegex(impl.ImplementationError,"gate output changed"):
            impl.request_changes(handoff["handoff_path"],"Fix reported problem.")

    def test_stale_spec_cannot_run_accept_or_request_corrections(self):
        handoff=self.prepare()
        result=impl.run_implementation(handoff["handoff_path"])
        prepared=self.prepare(authorization="Separate authorization to test stale start")
        self.fixture.spec.write_text("Revision two changes requirements")
        self.fixture.call("spec","--revision","2","--file",str(self.fixture.spec))
        with self.assertRaisesRegex(impl.ImplementationError,"stale"):
            impl.run_implementation(prepared["handoff_path"])
        with self.assertRaisesRegex(impl.ImplementationError,"stale"):
            impl.record_astra_review(handoff["handoff_path"],True,"Product review passed for old requirements.")
        with self.assertRaisesRegex(impl.ImplementationError,"stale"):
            impl.request_changes(handoff["handoff_path"],"Fix old requirements.")

    def test_permission_bypass_and_bash_grants_are_rejected(self):
        original=json.loads(self.config.read_text())
        changed=json.loads(self.config.read_text())
        index=changed["extra_args"].index("--permission-mode")
        changed["extra_args"][index+1]="bypassPermissions"
        self.config.write_text(json.dumps(changed))
        with self.assertRaisesRegex(impl.ImplementationError,"permission-mode auto"):
            self.prepare()
        original["extra_args"] += ["--allowedTools","Read,Edit,Bash"]
        self.config.write_text(json.dumps(original))
        with self.assertRaisesRegex(impl.ImplementationError,"Bash allow"):
            self.prepare()

    def test_exact_uuid_discovery_handles_hashed_directory_and_rejects_ambiguity_cwd(self):
        session=str(uuid.uuid4())
        configdir=self.root / "claude-storage"
        projects=configdir / "projects"
        location=projects / ("long-path-" + "a"*100 + "-private-hash") / (session+".jsonl")
        location.parent.mkdir(parents=True)
        event={"sessionId":session,"cwd":str(self.project),"message":{"content":[{"type":"thinking","thinking":"private canary"}]}}
        location.write_text(json.dumps(event)+"\n")
        other=projects / "unrelated" / (str(uuid.uuid4())+".jsonl")
        other.parent.mkdir()
        other.write_bytes(b"this unrelated transcript is deliberately malformed and must never be read")
        found=session_paths.find_session_transcript(configdir,session,self.project,projects / "wrong-prediction" / (session+".jsonl"))
        self.assertEqual(found,str(location.resolve()))
        long_prediction=projects / ("z"*300) / (session+".jsonl")
        self.assertEqual(session_paths.find_session_transcript(configdir,session,self.project,long_prediction),str(location.resolve()))
        with self.assertRaisesRegex(session_paths.SessionPathError,"cwd"):
            session_paths.find_session_transcript(configdir,session,self.root / "different")
        duplicate=other.parent / location.name
        duplicate.write_text(location.read_text())
        with self.assertRaisesRegex(session_paths.SessionPathError,"found 2"):
            session_paths.find_session_transcript(configdir,session,self.project)

    def test_staged_blob_changes_are_bound_even_when_working_bytes_match(self):
        handoff=self.prepare()
        impl.run_implementation(handoff["handoff_path"])
        worktree=Path(handoff["worktree_path"])
        original=impl.candidate_snapshot(worktree)
        target=worktree / "baseline.txt"
        previous=target.read_bytes()
        target.write_text("staged different content")
        subprocess.run(["git","-C",str(worktree),"add","baseline.txt"],check=True)
        target.write_bytes(previous)
        changed=impl.candidate_snapshot(worktree)
        self.assertEqual(original["entries"],changed["entries"])
        self.assertNotEqual(original["sha256"],changed["sha256"])
        with self.assertRaisesRegex(impl.ImplementationError,"changed after"):
            impl.record_astra_review(handoff["handoff_path"],True,"Product behavior checked.")

    def test_duplicate_sensitive_flags_and_variadic_bash_grant_cannot_override_pins(self):
        original=json.loads(self.config.read_text())
        for extra in (["--permission-mode","bypassPermissions"],["--effort","low"],
                      ["--settings",'{"disableAllHooks":false}'],["--allowedTools","Read","--allowedTools","Bash"]):
            config=dict(original,extra_args=original["extra_args"]+extra)
            self.config.write_text(json.dumps(config))
            with self.assertRaisesRegex(impl.ImplementationError,"Duplicate"):
                self.prepare()
        self.config.write_text(json.dumps(dict(original,extra_args=original["extra_args"]+["--allowedTools","Read","Bash"])))
        with self.assertRaisesRegex(impl.ImplementationError,"Bash allow"):
            self.prepare()

    def test_auth_namespace_override_is_preserved_only_when_explicitly_configured(self):
        handoff=self.prepare()
        impl.run_implementation(handoff["handoff_path"])
        self.assertIsNone(self.calls()[0]["config_dir_override"])
        config=json.loads(self.config.read_text())
        config["claude_config_dir_override"]=str(self.root / "custom-auth-namespace")
        self.config.write_text(json.dumps(config))
        custom=self.prepare(authorization="Authorized custom namespace fixture")
        impl.run_implementation(custom["handoff_path"])
        self.assertEqual(self.calls()[1]["config_dir_override"],config["claude_config_dir_override"])

    def test_timeout_and_signal_kill_own_child_and_block_future_runs(self):
        self.mode.write_text("timeout")
        config=json.loads(self.config.read_text())
        config["timeout_seconds"]=1
        self.config.write_text(json.dumps(config))
        handoff=self.prepare()
        result=impl.run_implementation(handoff["handoff_path"])
        self.assertEqual(result["phase"],"blocked")
        child=self.calls()[0]["pid"]
        with self.assertRaises(ProcessLookupError): os.kill(child,0)
        config["timeout_seconds"]=30
        self.config.write_text(json.dumps(config))
        second=self.prepare(authorization="A separate authorized signal fixture")
        process=subprocess.Popen([sys.executable,str(Path(impl.__file__)),"run","--handoff",second["handoff_path"]],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            deadline=time.monotonic()+5
            while len(self.calls())<2 and time.monotonic()<deadline: time.sleep(0.01)
            self.assertEqual(len(self.calls()),2)
            process.send_signal(signal.SIGTERM)
            output,errors=process.communicate(timeout=5)
            self.assertEqual(process.returncode,2,errors)
            self.assertEqual(json.loads(output)["phase"],"blocked")
            with self.assertRaises(ProcessLookupError): os.kill(self.calls()[1]["pid"],0)
        finally:
            if process.poll() is None:
                process.kill(); process.communicate()


if __name__ == "__main__":
    unittest.main()
