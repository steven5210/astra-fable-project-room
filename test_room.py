"""Process-level tests using an explicitly fake Claude executable; no model calls."""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest


ROOM_CLI = Path(__file__).with_name("room.py")

FAKE_CLAUDE = r'''#!/usr/bin/env python3
import datetime, json, os, pathlib, sys, time
args = sys.argv[1:]
root = pathlib.Path(__file__).parent
(root / "child.pid").write_text(str(os.getpid()))
mode = (root / "mode.txt").read_text().strip()
prompt = sys.stdin.read()
packet = json.loads(prompt.split("REVIEW PACKET (JSON):\n", 1)[1])
session = args[args.index("--resume") + 1] if "--resume" in args else args[args.index("--session-id") + 1]
with (root / "calls.jsonl").open("a") as log:
    log.write(json.dumps({"argv": args, "packet": packet, "session": session}) + "\n")
if mode == "timeout":
    print("partial output", flush=True)
    time.sleep(30)
if mode == "malformed":
    print("not JSON")
    sys.exit(0)
if mode == "json-list":
    print("[]")
    sys.exit(0)
review = {
    "interpretation": "I independently interpret this as a read-only feature review.",
    "findings": ["Clarify the acceptance criterion."] if mode == "changes" else [],
    "decision": "changes_required" if mode == "changes" else "accept",
    "spec_revision": packet["spec_revision"], "spec_sha256": packet["spec_sha256"],
}
result = {
    "type": "result", "subtype": "success", "is_error": mode == "error",
    "session_id": "different-session" if mode == "wrong-session" else session,
    "modelUsage": {"unexpected-model" if mode == "wrong-model" else "fable-exact-test": {"inputTokens": 1}},
    "result": "Read-only review complete.", "structured_output": review,
}
if mode == "wrong-hash":
    review["spec_sha256"] = "0" * 64
if mode == "wrong-revision":
    review["spec_revision"] += 1
if mode == "missing-structured":
    del result["structured_output"]
if mode == "invalid-structured":
    review["findings"] = "not a list"
if mode == "missing-model":
    del result["modelUsage"]
if mode == "bad-terminal":
    result["terminal_reason"] = "max_turns"
if mode.startswith("mixed"):
    result["modelUsage"] = {"fable-exact-test": {"outputTokens": 100}, "helper-test": {"outputTokens": 2}}
    transcript_input = dict(review)
    if mode == "mixed-mismatched-output":
        transcript_input["decision"] = "changes_required"
    event = {
        "type": "assistant", "sessionId": "another-session" if mode == "mixed-wrong-session" else session,
        "timestamp": "2000-01-01T00:00:00+00:00" if mode == "mixed-stale" else
                     "2100-01-01T00:00:00+00:00" if mode == "mixed-future" else datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "message": {"id": "fixture-message-id", "model": "helper-test" if mode == "mixed-wrong-primary" else "fable-exact-test",
                    "content": [{"type": "thinking", "thinking": "PRIVATE_THINKING_MUST_NOT_BE_EXPOSED"},
                                {"type": "tool_use", "id": "fixture-tool-id", "name": "StructuredOutput", "input": transcript_input}]},
    }
    if mode == "mixed-newer-output":
        newer_input = dict(review, decision="changes_required")
        event["message"]["content"].append({"type": "tool_use", "id": "fixture-newer-tool", "name": "StructuredOutput", "input": newer_input})
    if mode != "mixed-missing":
        with (root / (session + ".jsonl")).open("a") as evidence:
            evidence.write(json.dumps(event) + "\n")
print(json.dumps(result))
if mode in ("nonzero", "mixed-nonzero"):
    sys.exit(7)
'''


class RoomTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="project-room-test-")
        self.root = Path(self.temp.name)
        self.room = self.root / "room"
        self.fake = self.root / "fake-claude"
        self.fake.write_text(FAKE_CLAUDE.replace("#!/usr/bin/env python3", f"#!{sys.executable}", 1))
        self.fake.chmod(0o700)
        self.mode = self.root / "mode.txt"
        self.mode.write_text("accept")
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({
            "claude_bin": str(self.fake), "model": "fable-requested-test",
            "expected_model_ids": ["fable-exact-test"], "extra_args": ["--fixture-flag", "literal $(never-run)"],
            "timeout_seconds": 5,
        }))
        self.spec = self.root / "spec.md"
        self.spec.write_bytes(b"# Pilot\r\nExact bytes, including CRLF.\r\n")
        self.message = self.root / "message.md"
        self.message.write_text("Review this spec independently; do not implement anything.\n")
        self.call("init", "--config", str(self.config))
        self.call("spec", "--revision", "1", "--file", str(self.spec))

    def tearDown(self):
        self.temp.cleanup()

    def command(self, *args):
        return [sys.executable, str(ROOM_CLI), "--room", str(self.room), *args]

    def call(self, *args, expected=0):
        completed = subprocess.run(self.command(*args), capture_output=True, text=True, timeout=15)
        self.assertEqual(completed.returncode, expected, msg=completed.stdout + completed.stderr)
        if completed.stdout:
            return json.loads(completed.stdout)
        return json.loads(completed.stderr)

    def ask(self, request="review-1", revision=1, expected=0, timeout=None, evidence=None):
        args = ["ask", "--revision", str(revision), "--message-file", str(self.message), "--request-id", request]
        if timeout is not None:
            args += ["--timeout", str(timeout)]
        if evidence is not None:
            args += ["--session-transcript", str(evidence)]
        return self.call(*args, expected=expected)

    def evidence_path(self):
        return self.root / (self.call("status")["session_id"] + ".jsonl")

    def reconcile(self, evidence, expected=0):
        return self.call("reconcile", "--request-id", "review-1", "--session-transcript", str(evidence),
                         "--note-file", str(self.message), expected=expected)

    def calls(self):
        log = self.root / "calls.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def approve(self, revision=1):
        return self.call("record", "--sender", "astra", "--kind", "approval", "--revision", str(revision), "--file", str(self.message))

    def test_restart_resumes_same_session_and_exact_model(self):
        initial = self.ask()
        following = self.ask("review-2")  # Every CLI call is a fresh Python process.
        self.assertEqual(initial["status"], "completed")
        self.assertEqual(initial["session_id"], following["session_id"])
        first, second = self.calls()
        self.assertIn("--session-id", first["argv"])
        self.assertNotIn("--resume", first["argv"])
        self.assertIn("--resume", second["argv"])
        self.assertNotIn("--session-id", second["argv"])
        self.assertEqual(first["argv"][first["argv"].index("--model") + 1], "fable-requested-test")
        self.assertIn("literal $(never-run)", first["argv"])
        self.assertEqual(first["packet"]["spec_text"].encode(), self.spec.read_bytes())
        self.assertEqual(first["packet"]["spec_sha256"], hashlib.sha256(self.spec.read_bytes()).hexdigest())
        self.assertEqual(initial["actual_models"], ["fable-exact-test"])
        self.assertTrue(Path(initial["stdout_path"]).is_file())
        self.assertTrue(self.call("status")["session_started"])

    def test_identical_request_returns_recorded_result_without_invocation(self):
        first = self.ask()
        self.mode.write_text("error")
        again = self.ask()
        self.assertFalse(first["duplicate"])
        self.assertTrue(again["duplicate"])
        self.assertEqual(first["result"], again["result"])
        self.assertEqual(len(self.calls()), 1)

    def test_different_payload_for_same_request_is_rejected(self):
        self.ask()
        self.message.write_text("Different request bytes")
        rejected = self.ask(expected=2)
        self.assertIn("different exact payload", rejected["error"])
        self.assertEqual(len(self.calls()), 1)

    def test_spec_revisions_immutable_and_snapshot_tampering_detected(self):
        original = self.call("spec", "--revision", "1", "--file", str(self.spec))
        self.assertTrue(original["existing"])
        self.spec.write_text("Changed content")
        rejected = self.call("spec", "--revision", "1", "--file", str(self.spec), expected=2)
        self.assertIn("immutable", rejected["error"])
        snapshot = next((self.room / "specs").iterdir())
        snapshot.write_text("Corrupted")
        self.assertIn("modified", self.ask(expected=2)["error"])
        self.assertEqual(len(self.calls()), 0)

    def test_agreement_requires_current_revision_both_approvals(self):
        self.ask()
        self.assertFalse(self.call("status")["agreement"])
        self.approve()
        self.assertTrue(self.call("status")["agreement"])
        self.assertFalse(self.call("status")["implementation_authorized"])
        self.spec.write_text("Revision two adds a concrete acceptance criterion.\n")
        self.call("spec", "--revision", "2", "--file", str(self.spec))
        current = self.call("status")
        self.assertFalse(current["agreement"])
        self.assertFalse(current["astra_approved"])
        self.assertFalse(current["fable_accepted"])
        rejected = self.call("record", "--sender", "astra", "--kind", "approval", "--revision", "1", "--file", str(self.message), expected=2)
        self.assertIn("Stale revision", rejected["error"])
        self.assertIn("Stale revision", self.ask("stale-review", revision=1, expected=2)["error"])
        self.ask("review-v2", revision=2)
        self.approve(2)
        self.assertTrue(self.call("status")["agreement"])

    def test_latest_review_can_withdraw_acceptance(self):
        self.ask()
        self.approve()
        self.mode.write_text("changes")
        self.ask("review-followup")
        self.assertFalse(self.call("status")["fable_accepted"])
        self.assertFalse(self.call("status")["agreement"])

    def test_identity_spec_and_process_failures_never_approve(self):
        for mode in ("wrong-model", "missing-model", "wrong-session", "wrong-hash", "wrong-revision", "error", "nonzero", "bad-terminal"):
            with self.subTest(mode=mode):
                original_room = self.room
                self.room = self.root / f"room-{mode}"
                self.call("init", "--config", str(self.config))
                self.call("spec", "--revision", "1", "--file", str(self.spec))
                self.approve()
                self.mode.write_text(mode)
                result = self.ask(expected=2)
                self.assertEqual(result["status"], "failed")
                self.assertFalse(self.call("status")["fable_accepted"])
                self.assertFalse(self.call("status")["agreement"])
                count = len(self.calls())
                self.mode.write_text("accept")
                self.assertIn("will not be replayed", self.ask(expected=2)["error"])
                self.assertIn("blocked", self.ask("new-request", expected=2)["error"])
                self.assertEqual(len(self.calls()), count)
                self.room = original_room

    def test_malformed_outputs_are_uncertain_and_cannot_be_retried(self):
        for mode in ("malformed", "json-list", "missing-structured", "invalid-structured"):
            with self.subTest(mode=mode):
                original_room = self.room
                self.room = self.root / f"room-{mode}"
                self.call("init", "--config", str(self.config))
                self.call("spec", "--revision", "1", "--file", str(self.spec))
                self.mode.write_text(mode)
                result = self.ask(expected=2)
                self.assertEqual(result["status"], "uncertain")
                self.assertFalse(self.call("status")["agreement"])
                count = len(self.calls())
                self.assertIn("blocked", self.ask("another", expected=2)["error"])
                self.assertEqual(len(self.calls()), count)
                self.room = original_room

    def test_timeout_captures_partial_output_and_blocks(self):
        self.mode.write_text("timeout")
        result = self.ask(expected=2, timeout=2)
        self.assertEqual(result["status"], "uncertain")
        self.assertIn("partial output", Path(result["stdout_path"]).read_text())
        self.assertIn("blocked", self.ask("another", expected=2)["error"])
        self.assertEqual(len(self.calls()), 1)

    def interrupt_and_verify(self, sig):
        self.mode.write_text("timeout")
        process = subprocess.Popen(self.command("ask", "--revision", "1", "--message-file", str(self.message), "--request-id", "interrupt"), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 5
            while not self.calls() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(self.calls())
            process.send_signal(sig)
            output, errors = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 2, errors)
            self.assertEqual(json.loads(output)["status"], "uncertain")
            child_pid = int((self.root / "child.pid").read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)
            self.assertIn("blocked", self.ask("after-interrupt", expected=2)["error"])
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_interrupt_records_uncertainty(self):
        self.interrupt_and_verify(signal.SIGINT)

    def test_sigterm_records_uncertainty_and_terminates_own_child(self):
        self.interrupt_and_verify(signal.SIGTERM)

    def test_orphan_pending_turn_is_uncertain_and_not_replayed(self):
        self.ask()
        with sqlite3.connect(self.room / "room.sqlite3") as db:
            db.execute("UPDATE turns SET status='pending', result_json=NULL")
        duplicate = self.ask(expected=2)
        self.assertIn("uncertain", duplicate["error"])
        result = self.ask("new-request", expected=2)
        self.assertIn("uncertain", result["error"])
        self.assertEqual(self.call("status")["blocking_turns"][0]["status"], "uncertain")
        self.assertEqual(len(self.calls()), 1)

    def test_room_lock_prevents_overlap(self):
        with (self.room / ".lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.ask(expected=2)
            self.assertIn("Another room mutation", result["error"])
            self.assertEqual(len(self.calls()), 0)
        self.ask()

    def test_transcript_contains_messages_specs_decisions_and_identity(self):
        self.call("record", "--sender", "user", "--kind", "message", "--revision", "1", "--file", str(self.message))
        self.ask()
        self.approve()
        path = self.root / "export" / "transcript.md"
        self.call("transcript", "--file", str(path))
        output = path.read_text()
        self.assertIn("Exact-revision agreement: **True**", output)
        self.assertIn("User · message", output)
        self.assertIn("Astra · approval", output)
        self.assertIn("fable-exact-test", output)
        self.assertIn('"decision": "accept"', output)
        self.assertIn("Read-only review complete.", output)

    def test_configuration_changes_and_identity_overrides_are_rejected(self):
        config = json.loads(self.config.read_text())
        config["model"] = "different-model"
        self.config.write_text(json.dumps(config))
        self.assertIn("Configuration differs", self.ask(expected=2)["error"])
        config["extra_args"] = ["--fallback-model=other-model"]
        self.config.write_text(json.dumps(config))
        self.assertIn("may not override", self.ask(expected=2)["error"])
        self.assertEqual(len(self.calls()), 0)

    def test_referenced_policy_and_mcp_file_contents_are_pinned(self):
        for option in ("--append-system-prompt-file", "--mcp-config", "--settings"):
            with self.subTest(option=option):
                reference = self.root / "reference.json"
                reference.write_text('{}\n')
                config = json.loads(self.config.read_text())
                config["extra_args"] = [option, str(reference)]
                self.config.write_text(json.dumps(config))
                self.room = self.root / f"room-{option}"
                self.call("init", "--config", str(self.config))
                self.call("spec", "--revision", "1", "--file", str(self.spec))
                reference.write_text('{"changed":true}\n')
                self.assertIn("Configuration differs", self.ask(expected=2)["error"])
                self.assertEqual(len(self.calls()), 0)

    def test_three_turn_cap_does_not_count_cached_replays(self):
        self.ask()
        self.ask()
        self.ask("second")
        self.ask("third")
        self.assertIn("3-turn review limit", self.ask("fourth", expected=2)["error"])
        self.assertEqual(len(self.calls()), 3)
        self.assertTrue(self.ask()["duplicate"])

    def test_nonfinite_timeout_rejected_before_starting_process(self):
        for value in (float("nan"), float("inf"), -1):
            config = json.loads(self.config.read_text())
            config["timeout_seconds"] = value
            self.config.write_text(json.dumps(config))
            self.assertIn("finite", self.ask(expected=2)["error"])
        for value in ("nan", "inf"):
            completed = subprocess.run(self.command("ask", "--revision", "1", "--message-file", str(self.message), "--request-id", "bad-timeout", "--timeout", value), capture_output=True, text=True)
            self.assertEqual(completed.returncode, 2)
            self.assertIn("finite", completed.stderr)
        self.assertEqual(len(self.calls()), 0)

    def test_provider_override_rejected_without_exposing_value(self):
        environment = dict(os.environ, ANTHROPIC_API_KEY="do-not-print-this-secret")
        completed = subprocess.run(self.command("ask", "--revision", "1", "--message-file", str(self.message), "--request-id", "bad-provider"), env=environment, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 2)
        self.assertIn("ANTHROPIC_API_KEY", completed.stderr)
        self.assertNotIn("do-not-print-this-secret", completed.stderr + completed.stdout)
        self.assertEqual(len(self.calls()), 0)

    def test_mixed_usage_requires_exact_primary_producer_evidence(self):
        self.mode.write_text("mixed-good")
        evidence = self.evidence_path()
        result = self.ask(evidence=evidence)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["primary_model"], "fable-exact-test")
        self.assertEqual(result["auxiliary_models"], ["helper-test"])
        self.assertEqual(result["actual_models"], ["fable-exact-test", "helper-test"])
        self.assertEqual(result["return_code"], 0)
        self.assertEqual(result["identity_evidence"]["message_id"], "fixture-message-id")
        self.assertNotIn("PRIVATE_THINKING", json.dumps(result))
        self.assertNotIn("PRIVATE_THINKING", json.dumps(self.call("status")))
        self.approve()
        self.assertTrue(self.call("status")["agreement"])

    def test_mixed_wrong_primary_missing_mismatched_or_outside_turn_evidence_rejects(self):
        for mode in ("mixed-wrong-primary", "mixed-missing", "mixed-mismatched-output", "mixed-wrong-session", "mixed-stale", "mixed-future", "mixed-newer-output"):
            with self.subTest(mode=mode):
                self.room = self.root / mode
                self.call("init", "--config", str(self.config))
                self.call("spec", "--revision", "1", "--file", str(self.spec))
                self.mode.write_text(mode)
                evidence = self.evidence_path()
                result = self.ask(expected=2, evidence=evidence)
                self.assertEqual(result["status"], "failed")
                self.assertIn("Model identity verification failed", result["error"])
                self.assertFalse(self.call("status")["agreement"])
                self.assertIn("still fails verification", self.reconcile(evidence, expected=2)["error"])

    def test_reconcile_only_revalidates_saved_output_and_preserves_original_failure(self):
        self.mode.write_text("mixed-good")
        evidence = self.evidence_path()
        failed = self.ask(expected=2)  # Evidence is not implicitly searched or trusted.
        original_bytes = Path(failed["stdout_path"]).read_bytes()
        reconciled = self.reconcile(evidence)
        self.assertTrue(reconciled["reconciled"])
        self.assertEqual(reconciled["return_code"], 0)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(Path(failed["stdout_path"]).read_bytes(), original_bytes)
        self.assertEqual(reconciled["session_id"], failed["session_id"])
        self.assertTrue(self.call("status")["session_started"])
        with sqlite3.connect(self.room / "room.sqlite3") as db:
            original, note = db.execute("SELECT original_turn_json,note FROM reconciliations").fetchone()
            self.assertEqual(json.loads(original)["error"], failed["error"])
            self.assertEqual(json.loads(original)["status"], "failed")
            self.assertEqual(note, self.message.read_bytes())
        self.assertTrue(self.ask()["duplicate"])
        following = self.ask("following", evidence=evidence)
        self.assertEqual(following["session_id"], failed["session_id"])
        self.assertIn("--resume", self.calls()[1]["argv"])
        self.assertEqual(len(self.calls()), 2)

    def test_legacy_reconcile_records_control_flow_inference_without_inventing_measured_code(self):
        self.mode.write_text("mixed-good")
        evidence = self.evidence_path()
        failed = self.ask(expected=2)
        legacy_error = "Actual model identity is missing or unexpected: ['fable-exact-test', 'helper-test']"
        with sqlite3.connect(self.room / "room.sqlite3") as db:
            db.execute("UPDATE turns SET return_code=NULL, stdout_sha256=NULL, error=?", (legacy_error,))
        reconciled = self.reconcile(evidence)
        self.assertIsNone(reconciled["return_code"])
        self.assertIn("control flow", reconciled["return_code_basis"])
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.call("status")["reconciliations"][0]["return_code_basis"], reconciled["return_code_basis"])
        path = self.root / "reconciled.md"
        self.call("transcript", "--file", str(path))
        self.assertIn(legacy_error, path.read_text())
        self.assertNotIn("PRIVATE_THINKING", path.read_text())

    def test_reconcile_refuses_nonzero_unknown_tampered_and_unrelated_failure(self):
        for mode in ("mixed-nonzero", "malformed", "wrong-session", "wrong-hash", "error"):
            with self.subTest(mode=mode):
                self.room = self.root / mode
                self.call("init", "--config", str(self.config))
                self.call("spec", "--revision", "1", "--file", str(self.spec))
                self.mode.write_text(mode)
                evidence = self.evidence_path()
                self.ask(expected=2)
                count = len(self.calls())
                self.reconcile(evidence, expected=2)
                self.assertEqual(len(self.calls()), count)
                self.assertFalse(self.call("status")["session_started"])
        self.room = self.root / "tamper"
        self.call("init", "--config", str(self.config))
        self.call("spec", "--revision", "1", "--file", str(self.spec))
        self.mode.write_text("mixed-good")
        evidence = self.evidence_path()
        failed = self.ask(expected=2)
        output = Path(failed["stdout_path"])
        output.write_bytes(output.read_bytes() + b"\n")
        self.assertIn("stdout changed", self.reconcile(evidence, expected=2)["error"])


if __name__ == "__main__":
    unittest.main()
