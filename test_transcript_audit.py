"""Read-only transcript audit: exact-session tool-use counts, child attribution, completeness codes; no model, no network."""

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

import recovery
import session_paths
import transcript_audit
from test_deepseek_wiring import WiringFixture, implementation_fake
from test_project_room import ROOT


def record(kind, session, cwd, stamp, blocks, **extra):
    value = {"type": kind, "sessionId": session, "cwd": cwd, "timestamp": stamp, "uuid": extra.pop("uuid", "u-" + stamp),
             "message": {"role": "assistant" if kind == "assistant" else "user", "model": extra.pop("model", "claude-fable-5-1"), "content": blocks}}
    value.update(extra)
    return json.dumps(value)


def tool_use(name, identifier="toolu_x", **arguments):
    return {"type": "tool_use", "id": identifier, "name": name, "input": arguments}


class TranscriptAuditTests(WiringFixture):
    def setUp(self):
        super().setUp()
        self.init_git()
        self.configure("none")
        self.audit_room, self.audit_root = self.open_room("Audited feature")
        self.consensus(self.audit_room)
        self.handoff = self.service.room_handoff(self.audit_room, 1, "Build it through independent review.",
                                                 [[sys.executable, "-c", "from pathlib import Path; assert Path('feature.txt').read_text() == 'implemented\\n'"]])
        self.fake.write_text(implementation_fake())
        (self.base / "implementation-mode.txt").write_text("normal")
        job = self.service.room_implementation_submit(self.audit_room, self.handoff["handoff_id"], "implement-1")
        terminal = self.service.room_job_status(job["id"], 30)
        self.assertEqual(terminal["status"], "succeeded", terminal)
        self.session = terminal["result"]["implementation_session_id"]
        self.transcript = Path(terminal["result"]["identity_evidence"]["path"])
        self.worktree = self.handoff["worktree_path"]
        self.subagents = self.transcript.parent / self.session / "subagents"
        receipt = json.loads((Path(self.handoff["handoff_path"]).parent / "attempts" / "0001" / "process-start.json").read_text())
        self.inside = receipt["started_at"]  # the attempt interval is closed, so its start instant lies inside it
        self.before = "2000-01-01T00:00:00+00:00"

    def audit(self, attempt=1):
        return transcript_audit.audit(self.home, self.audit_room, self.handoff["handoff_id"], attempt)

    def append_parent(self, *lines):
        with self.transcript.open("a") as handle:
            for line in lines:
                handle.write(line + "\n")

    def child(self, agent, *lines):
        self.subagents.mkdir(parents=True, exist_ok=True)
        path = self.subagents / f"agent-{agent}.jsonl"
        with path.open("a") as handle:
            for line in lines:
                handle.write(line + "\n")
        return path

    def test_clean_attempt_is_complete_with_zero_qwen_counts_and_no_excerpts(self):
        report = self.audit()
        self.assertTrue(report["complete"], report)
        self.assertTrue(report["qwen_absent"])
        self.assertEqual(report["counts_total"]["qwen_local"], 0)
        self.assertEqual(report["parent"]["sha256"], recovery.room.sha(self.transcript.read_bytes()))
        self.assertEqual(report["parent"]["bytes"], self.transcript.stat().st_size)
        self.assertEqual(report["session_id"], self.session)
        self.assertIsNotNone(report["attempt_interval"])
        self.assertEqual(report["children"], [])
        serialized = json.dumps(report)
        for private in ("DO_NOT_EXPOSE_PRIVATE_THINKING", "IMPLEMENTATION PACKET", str(self.transcript), self.worktree):
            self.assertNotIn(private, serialized)
        self.assertEqual(len(self.implementation_calls()), 1, "the audit launched nothing")

    def test_qwen_use_in_parent_or_any_child_is_counted_and_attribution_is_reported_separately(self):
        self.append_parent(
            record("assistant", self.session, self.worktree, self.inside, [tool_use("mcp__qwen-local__qwen_submit", "toolu_q", task="PRIVATE_TASK_TEXT")]),
            record("assistant", self.session, self.worktree, self.inside, [tool_use("Agent", "toolu_a", subagent_type="sonnet-worker", prompt="PRIVATE_PROMPT")], uuid="launcher"),
            record("user", self.session, self.worktree, self.inside, [{"type": "tool_result", "tool_use_id": "toolu_a", "content": "launched"}],
                   toolUseResult={"status": "async_launched", "agentId": "child-one"}),
            record("assistant", self.session, self.worktree, self.before, [tool_use("mcp__deepseek__deepseek_submit", "toolu_d", task="PRIVATE_OLD_TASK")]))
        self.child("child-one", record("assistant", self.session, self.worktree, self.inside, [tool_use("Bash", "toolu_b", command="PRIVATE_COMMAND")],
                                       agentId="child-one", isSidechain=True, model="claude-sonnet-5"))
        self.child("stray", record("assistant", self.session, self.worktree, self.inside, [tool_use("mcp__qwen-local__qwen_ask", "toolu_s", question="PRIVATE_Q")],
                                   agentId="stray", isSidechain=True, model="claude-opus-5"))
        report = self.audit()
        self.assertTrue(report["complete"], report)
        self.assertFalse(report["qwen_absent"])
        self.assertEqual(report["counts_total"]["qwen_local"], 2)
        self.assertEqual(report["counts_in_attempt"]["qwen_local"], 2)
        self.assertEqual((report["counts_total"]["deepseek"], report["counts_in_attempt"]["deepseek"]), (1, 0), "history outside the attempt is counted conservatively but separately")
        self.assertEqual((report["counts_total"]["agent"], report["parent"]["counts"]["other"]), (1, 1 + 0))
        self.assertEqual((report["launched_children"], report["attributable_children"], report["unattributed_children"]), (1, 1, 1))
        by_handle = {child["handle"]: child for child in report["children"]}
        attributable = next(child for child in report["children"] if child["attributable"])
        stray = next(child for child in report["children"] if not child["attributable"])
        self.assertEqual((attributable["models"], attributable["counts"]["other"]), (["claude-sonnet-5"], 1))
        self.assertEqual((stray["counts"]["qwen_local"], stray["models"]), (1, ["claude-opus-5"]))
        self.assertEqual(len(by_handle), 2)
        serialized = json.dumps(report)
        for private in ("PRIVATE_TASK_TEXT", "PRIVATE_PROMPT", "PRIVATE_COMMAND", "PRIVATE_Q", "PRIVATE_OLD_TASK", "child-one", "stray", "toolu_"):
            self.assertNotIn(private, serialized)

    def launch(self, agent, tool_use_id, **extra):
        return record("user", self.session, self.worktree, self.inside, [{"type": "tool_result", "tool_use_id": tool_use_id, "content": "launched"}],
                      toolUseResult={"status": "async_launched", "agentId": agent}, **extra)

    def test_missing_grandchild_and_cross_file_mutation_never_certify_completeness(self):
        self.append_parent(record("assistant", self.session, self.worktree, self.inside, [tool_use("Agent", "toolu_c", subagent_type="sonnet-worker", prompt="PRIVATE")], uuid="launch-child"),
                           self.launch("child-one", "toolu_c"))
        self.child("child-one",
                   record("assistant", self.session, self.worktree, self.inside, [tool_use("Agent", "toolu_g", subagent_type="opus-reviewer", prompt="PRIVATE")],
                          agentId="child-one", isSidechain=True, model="claude-sonnet-5", uuid="launch-grandchild"),
                   self.launch("grandchild", "toolu_g", agentId="child-one", isSidechain=True))
        report = self.audit()
        self.assertFalse(report["complete"], report)
        self.assertIn("child_missing", report["reasons"])
        self.assertEqual((report["children_missing"], report["launched_children"], report["requested_descendants"]), (1, 1, 2))
        self.assertFalse(report["qwen_absent"])
        self.child("grandchild", record("assistant", self.session, self.worktree, self.inside, [tool_use("mcp__qwen-local__qwen_submit", "toolu_q", task="PRIVATE")],
                                        agentId="grandchild", isSidechain=True, model="claude-opus-5"))
        report = self.audit()
        self.assertTrue(report["complete"], report["reasons"])
        self.assertFalse(report["qwen_absent"], "a Qwen call two levels down is a violation")
        self.assertEqual((report["counts_total"]["qwen_local"], report["counts_in_attempt"]["qwen_local"]), (1, 1))
        self.assertEqual((report["attributable_children"], report["unattributed_children"], report["children_missing"]), (2, 0, 0))
        grandchild = next(child for child in report["children"] if child["counts"]["qwen_local"] == 1)
        self.assertEqual((grandchild["attributable"], grandchild["models"]), (True, ["claude-opus-5"]))
        self.assertEqual(next(child for child in report["children"] if child["models"] == ["claude-sonnet-5"])["launched_agent_handles"], [grandchild["handle"]])
        self.assertNotIn("grandchild", json.dumps(report))
        original = transcript_audit.scan_file

        def mutating(trigger, mutation):
            def scan(path, *args, **kwargs):
                result = original(path, *args, **kwargs)
                if kwargs.get("expected_agent") == trigger:
                    mutation()
                return result
            return scan
        late = record("assistant", self.session, self.worktree, self.inside, [tool_use("Bash", "toolu_late", command="PRIVATE")], uuid="late")
        with self.subTest("parent grows while a child is scanned"):
            with mock.patch.object(transcript_audit, "scan_file", mutating("child-one", lambda: self.append_parent(late))):
                report = self.audit()
            self.assertIn("parent_changed_during_scan", report["reasons"])
            self.assertFalse(report["complete"])
            self.assertTrue(self.audit()["complete"], "the same bytes are complete once nothing moves")
        with self.subTest("an earlier child grows while a later one is scanned"):
            grows = lambda: self.child("child-one", record("assistant", self.session, self.worktree, self.inside, [tool_use("Read", "toolu_r", file_path="PRIVATE")],
                                                           agentId="child-one", isSidechain=True))
            with mock.patch.object(transcript_audit, "scan_file", mutating("grandchild", grows)):
                report = self.audit()
            self.assertIn("child_changed_during_scan", report["reasons"])
            self.assertFalse(report["complete"])
            self.assertTrue(self.audit()["complete"])
        with self.subTest("a child file appears during the scan"):
            appears = lambda: self.child("late-arrival", record("assistant", self.session, self.worktree, self.inside, [tool_use("mcp__qwen-local__qwen_ask", "toolu_l", question="PRIVATE")],
                                                                agentId="late-arrival", isSidechain=True))
            with mock.patch.object(transcript_audit, "scan_file", mutating("child-one", appears)):
                report = self.audit()
            self.assertIn("children_dir_changed", report["reasons"])
            self.assertFalse(report["complete"])
            self.assertFalse(report["qwen_absent"])
            (self.subagents / "agent-late-arrival.jsonl").unlink()
        with self.subTest("a listed child disappears during the scan"):
            gone = self.subagents / "agent-grandchild.jsonl"
            saved = gone.read_bytes()
            with mock.patch.object(transcript_audit, "scan_file", mutating("child-one", gone.unlink)):
                report = self.audit()
            self.assertIn("children_dir_changed", report["reasons"])
            self.assertFalse(report["complete"])
            self.assertFalse(report["qwen_absent"])
            gone.write_bytes(saved)
        self.assertTrue(self.audit()["complete"])
        self.assertEqual(len(self.implementation_calls()), 1, "the audit launched nothing")

    def test_incomplete_evidence_never_claims_absence(self):
        with self.subTest("missing child"):
            self.append_parent(record("user", self.session, self.worktree, self.inside, [{"type": "tool_result", "tool_use_id": "toolu_m", "content": "launched"}],
                                      toolUseResult={"status": "async_launched", "agentId": "never-written"}))
            report = self.audit()
            self.assertFalse(report["complete"])
            self.assertIn("child_missing", report["reasons"])
            self.assertEqual(report["children_missing"], 1)
            self.assertFalse(report["qwen_absent"])
        self.child("never-written", record("assistant", self.session, self.worktree, self.inside, [tool_use("Read", "toolu_r", file_path="x")], agentId="never-written", isSidechain=True))
        self.assertTrue(self.audit()["complete"])
        with self.subTest("malformed parent record"):
            self.append_parent("{not json")
            report = self.audit()
            self.assertFalse(report["complete"])
            self.assertIn("transcript_unparsable", report["reasons"], "session metadata validation refuses the parent before any count is claimed")
        content = self.transcript.read_text().splitlines()
        self.transcript.write_text("\n".join(line for line in content if line != "{not json") + "\n")
        self.assertTrue(self.audit()["complete"])
        with self.subTest("malformed child record"):
            path = self.child("broken", "{not json")
            report = self.audit()
            self.assertFalse(report["complete"])
            self.assertIn("child_malformed_records", report["reasons"])
            path.unlink()
        with self.subTest("growing source"):
            original = transcript_audit._snapshot
            calls = []

            def growing(fd):
                value = original(fd)
                calls.append(fd)
                return (value[0] + 1, value[1], value[2]) if len(calls) == 2 else value
            with mock.patch.object(transcript_audit, "_snapshot", growing):
                report = self.audit()
            self.assertIn("source_growing", report["reasons"])
            self.assertFalse(report["complete"])
        with self.subTest("oversized"):
            with mock.patch.object(recovery, "TRANSCRIPT_LIMIT", 64):
                report = self.audit()
            self.assertFalse(report["complete"])
            self.assertTrue(any(reason.startswith("transcript_") for reason in report["reasons"]), report["reasons"])
        with self.subTest("overlong record"):
            with mock.patch.object(transcript_audit, "RECORD_LIMIT", 64):
                report = self.audit()
            self.assertIn("overlong_records", report["reasons"])
            self.assertFalse(report["complete"])
        with self.subTest("attempt in progress"):
            receipt = Path(self.handoff["handoff_path"]).parent / "attempts" / "0001" / "process-result.json"
            saved = receipt.read_bytes()
            receipt.unlink()
            report = self.audit()
            self.assertFalse(report["complete"])
            self.assertIn("attempt_in_progress_or_result_receipt_missing", report["reasons"])
            self.assertIsNone(report["attempt_interval"])
            self.assertEqual(report["counts_in_attempt"]["other"], 0)
            receipt.write_bytes(saved)
        with self.subTest("unknown attempt"):
            report = self.audit(attempt=7)
            self.assertFalse(report["complete"])
        with self.subTest("symlinked child rejected"):
            (self.subagents / "agent-linked.jsonl").symlink_to(self.transcript)
            report = self.audit()
            self.assertIn("child_path_rejected", report["reasons"])
            (self.subagents / "agent-linked.jsonl").unlink()
        with self.subTest("ambiguous transcript"):
            duplicate = self.transcript.parent.parent / "other-directory" / self.transcript.name
            duplicate.parent.mkdir()
            duplicate.write_bytes(self.transcript.read_bytes())
            report = self.audit()
            self.assertFalse(report["complete"])
            self.assertIn("transcript_missing_or_ambiguous", report["reasons"])
            duplicate.unlink()
        self.assertTrue(self.audit()["complete"])
        self.assertEqual(len(self.implementation_calls()), 1)

    def test_root_and_listing_races_and_large_directories_stay_incomplete_without_traceback(self):
        class RootRace:
            """The audit's own view of recovery: transcript location succeeds, but binding the located root for the scan fails."""

            def __getattr__(self, name):
                return getattr(recovery, name)

            @staticmethod
            def OwnedRoot(path, expected=None, kind="evidence"):
                if kind == "transcript":
                    raise recovery.ObservationError("transcript_unsafe", "synthetic race after location")
                return recovery.OwnedRoot(path, expected, kind)
        with mock.patch.object(transcript_audit, "recovery", RootRace()):
            report = self.audit()
        self.assertFalse(report["complete"])
        self.assertIn("transcript_root_unsafe", report["reasons"])
        self.assertFalse(report["qwen_absent"])
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        self.subagents.parent.mkdir(parents=True, exist_ok=True)
        self.subagents.symlink_to(elsewhere, target_is_directory=True)
        report = self.audit()
        self.assertIn("children_dir_unsafe", report["reasons"])
        self.assertFalse(report["complete"])
        self.subagents.unlink()
        self.child("real-child", record("assistant", self.session, self.worktree, self.inside, [tool_use("Read", "toolu_r", file_path="PRIVATE")], agentId="real-child", isSidechain=True))
        with mock.patch.object(transcript_audit, "_listing", side_effect=OSError(5, "synthetic listing failure")):
            report = self.audit()
        self.assertIn("children_dir_unreadable", report["reasons"])
        self.assertFalse(report["complete"])
        self.assertTrue(self.audit()["complete"])
        for index in range(transcript_audit.MAX_CHILD_FILES + 40):
            self.child(f"bulk-{index:04d}", record("assistant", self.session, self.worktree, self.inside, [tool_use("Bash", "toolu_b", command="PRIVATE_COMMAND")],
                                                    agentId=f"bulk-{index:04d}", isSidechain=True))
        report = self.audit()
        self.assertFalse(report["complete"])
        self.assertIn("children_truncated", report["reasons"])
        self.assertNotIn("children_dir_changed", report["reasons"], "two partial enumerations are never compared as definitive changes")
        self.assertLessEqual(len(report["children"]), transcript_audit.MAX_CHILD_FILES)
        self.assertFalse(report["qwen_absent"])
        serialized = json.dumps(report)
        for private in ("PRIVATE", "bulk-0000", "real-child", str(self.transcript)):
            self.assertNotIn(private, serialized)
        self.assertEqual(len(self.implementation_calls()), 1, "the audit launched nothing")

    def test_launch_evidence_and_attribution_are_reported_truthfully(self):
        self.append_parent(record("assistant", self.session, self.worktree, self.inside, [tool_use("Agent", "toolu_pending", subagent_type="fixture", prompt="PRIVATE")], uuid="pending"))
        report = self.audit()
        self.assertFalse(report["complete"], "an Agent launch with no result and no child is never certified complete")
        self.assertFalse(report["qwen_absent"])
        self.assertEqual(report["reasons"], ["launch_evidence_missing"])
        self.assertTrue(report["children_listing_complete"])
        self.assertEqual((report["launches"], report["unresolved_launches"], report["requested_descendants"], report["counts_total"]["agent"]), (1, 1, 0, 1))
        self.append_parent(record("user", self.session, self.worktree, self.inside, [{"type": "tool_result", "tool_use_id": "toolu_pending", "content": "PRIVATE failure", "is_error": True}], uuid="failed"))
        report = self.audit()
        self.assertTrue(report["complete"], report["reasons"])
        self.assertEqual((report["failed_launches"], report["unresolved_launches"]), (1, 0), "an error result proves no child started")
        self.append_parent(record("assistant", self.session, self.worktree, self.inside, [tool_use("Task", "toolu_task", prompt="PRIVATE")], uuid="task"),
                           record("user", self.session, self.worktree, self.inside, [{"type": "tool_result", "tool_use_id": "toolu_task", "content": "done"}], uuid="task-result"))
        report = self.audit()
        self.assertFalse(report["complete"])
        self.assertEqual(report["reasons"], ["launch_unidentified"], "a result that names no child leaves its transcript unlocatable")
        self.assertTrue(report["children_listing_complete"], "every listed child file was still read; only identity-level coverage is unproven")
        self.assertEqual((report["unidentified_launches"], report["counts_total"]["task"]), (1, 1))
        lines = self.transcript.read_text().splitlines()
        self.transcript.write_text("\n".join(line for line in lines if '"task"' not in line and '"task-result"' not in line) + "\n")
        self.assertTrue(self.audit()["complete"])
        self.append_parent(record("assistant", self.session, self.worktree, self.inside, [tool_use("Agent", "toolu_late", subagent_type="fixture", prompt="PRIVATE")], uuid="late"),
                           self.launch("late-child", "toolu_late", uuid="late-receipt"))
        report = self.audit()
        self.assertEqual(report["reasons"], ["child_missing"], "once the receipt names the child, its transcript is required")
        self.child("late-child", record("assistant", self.session, self.worktree, self.inside, [tool_use("Read", "toolu_r", file_path="PRIVATE")], agentId="late-child", isSidechain=True))
        report = self.audit()
        self.assertTrue(report["complete"], report["reasons"])
        self.assertTrue(report["qwen_absent"])
        self.assertFalse(report["attribution"]["ambiguous"])
        self.append_parent(record("assistant", self.session, self.worktree, self.inside, [tool_use("Agent", identifier=None, prompt="PRIVATE")], uuid="no-id"))
        report = self.audit()
        self.assertEqual(report["reasons"], ["launch_unidentified"], "a launch without a tool id can never be correlated")
        lines = self.transcript.read_text().splitlines()
        self.transcript.write_text("\n".join(line for line in lines if '"no-id"' not in line) + "\n")
        duplicate = record("assistant", self.session, self.worktree, self.inside, [tool_use("mcp__deepseek__deepseek_submit", "toolu_dup", task="PRIVATE")], uuid="dup-uuid")
        self.append_parent(duplicate, duplicate)
        self.append_parent(record("assistant", self.session, self.worktree, self.inside, [tool_use("Bash", "toolu_inline", command="PRIVATE")], uuid="inline", isSidechain=True))
        self.child("elsewhere-child", record("assistant", "another-session-uuid", self.worktree, self.inside, [tool_use("mcp__qwen-local__qwen_ask", "toolu_f", question="PRIVATE")],
                                             agentId="elsewhere-child", isSidechain=True))
        report = self.audit()
        self.assertTrue(report["complete"], report["reasons"])
        self.assertFalse(report["qwen_absent"], "Qwen use in a foreign-session record is still counted")
        self.assertEqual((report["counts_total"]["deepseek"], report["counts_total"]["qwen_local"]), (2, 1), "counts are conservative upper bounds")
        attribution = report["attribution"]
        self.assertEqual((attribution["duplicate_records"], attribution["foreign_session_records"], attribution["sidechain_records_in_parent"], attribution["ambiguous"]), (1, 1, 1, True))
        foreign = next(child for child in report["children"] if child["foreign_session_records"])
        self.assertEqual((foreign["attributable"], foreign["counts"]["qwen_local"]), (False, 1))
        serialized = json.dumps(report)
        for private in ("PRIVATE", "late-child", "elsewhere-child", "toolu_", "another-session-uuid"):
            self.assertNotIn(private, serialized)
        self.assertEqual(len(self.implementation_calls()), 1, "the audit launched nothing")

    def test_identifiers_are_validated_and_state_is_never_touched(self):
        with self.assertRaisesRegex(transcript_audit.AuditError, "Unknown handoff"):
            transcript_audit.audit(self.home, self.audit_room, "0" * 64, 1)
        with self.assertRaisesRegex(transcript_audit.AuditError, "64-hex"):
            transcript_audit.audit(self.home, self.audit_room, "../escape", 1)
        with self.assertRaisesRegex(transcript_audit.AuditError, "positive integer"):
            transcript_audit.audit(self.home, self.audit_room, self.handoff["handoff_id"], 0)
        handoff_dir = Path(self.handoff["handoff_path"]).parent
        before = {path: path.read_bytes() for path in handoff_dir.rglob("*") if path.is_file()}
        registry_before = (self.home / "registry.sqlite3").stat().st_mtime_ns
        with mock.patch.object(transcript_audit, "subprocess", create=True) as forbidden:
            forbidden.Popen.side_effect = AssertionError("no process may be spawned")
            self.assertTrue(self.audit()["complete"])
        self.assertEqual({path: path.read_bytes() for path in handoff_dir.rglob("*") if path.is_file()}, before)
        self.assertEqual((self.home / "registry.sqlite3").stat().st_mtime_ns, registry_before)
        self.assertEqual(self.service.room_status(self.audit_room)["handoffs"][0]["lineage"]["phase"], "awaiting_astra_review")

    def test_cli_entry_points_report_completeness_through_exit_codes(self):
        command = [sys.executable, str(ROOT / "project_room.py"), "--home", str(self.home), "transcript-audit", "--room", self.audit_room,
                   "--handoff", self.handoff["handoff_id"], "--attempt", "1"]
        process = subprocess.run(command, capture_output=True, text=True, timeout=60, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertTrue(json.loads(process.stdout)["complete"])
        module = [sys.executable, str(ROOT / "transcript_audit.py"), "--home", str(self.home), "--room", self.audit_room, "--handoff", self.handoff["handoff_id"], "--attempt", "1"]
        process = subprocess.run(module, capture_output=True, text=True, timeout=60, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(process.returncode, 0, process.stderr)
        self.append_parent("{not json")
        process = subprocess.run(module, capture_output=True, text=True, timeout=60, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(process.returncode, 1)
        self.assertFalse(json.loads(process.stdout)["complete"])
        process = subprocess.run(command, capture_output=True, text=True, timeout=60, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(process.returncode, 1, "the controller form shares the documented exit-code contract")
        self.assertFalse(json.loads(process.stdout)["complete"])
        self.assertEqual(process.stderr, "")
        process = subprocess.run(command[:-2] + ["--attempt", "1", "--handoff", "0" * 64], capture_output=True, text=True, timeout=60, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(process.returncode, 2)
        self.assertIn("Unknown handoff", process.stderr)
        process = subprocess.run(module[:-2] + ["--attempt", "1", "--handoff", "0" * 64], capture_output=True, text=True, timeout=60)
        self.assertEqual(process.returncode, 2)
        self.assertIn("Unknown handoff", process.stderr)
        self.assertEqual(len(self.implementation_calls()), 1)


if __name__ == "__main__":
    unittest.main()
