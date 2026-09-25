"""Counterexamples from the independent DeepSeek review of the native collector."""
import json
import unittest
from unittest.mock import patch

import ao_evidence_audit_io as audit_io
import ao_evidence_audit_native as native
from test_ao_evidence_audit import AuditFixture, completed_result
from test_ao_evidence_audit_independent import scanner, put, parent_interval, completed


class NativeReviewTests(unittest.TestCase):
    def launch(self):
        scan = scanner()
        put(scan, 1, 100, "user", "Continue.", origin={"kind": "human"})
        put(scan, 2, 110, "assistant", [{"type": "tool_use", "id": "launch", "name": "Agent",
                                       "input": {"prompt": "bounded task"}}])
        return scan

    def test_conflicting_result_cannot_hide_behind_one_valid_completion(self):
        good = {"type": "tool_result", "tool_use_id": "launch", "content": "done"}
        for bad in ({**good, "is_error": True}, {**good, "is_error": None}, good):
            for valid_first in (False, True):
                with self.subTest(bad=bad, valid_first=valid_first):
                    scan = self.launch()
                    entries = [(good, completed()), (bad, {"agentId": "child-1"})]
                    if not valid_first:
                        entries.reverse()
                    for number, (block, structured) in enumerate(entries, 3):
                        put(scan, number, 120, "user", [block], toolUseResult=structured)
                    candidates = native.launch_candidates(scan, parent_interval(scan)[0])
                    self.assertEqual(len(candidates), 1)
                    self.assertIsNone(candidates[0].agent_id)
                    self.assertIsNone(candidates[0].interval)

    def test_identical_completion_observations_deduplicate_without_losing_authority(self):
        scan = self.launch()
        for number in (3, 4):
            put(scan, number, 120, "user", [{"type": "tool_result", "tool_use_id": "launch", "content": "done"}],
                toolUseResult=completed())
        candidates = native.launch_candidates(scan, parent_interval(scan)[0])
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].agent_id, "child-1")
        self.assertIsNotNone(candidates[0].interval)
        self.assertIsNone(candidates[0].reason)

    def test_exact_duplicate_human_keeps_anchor_and_scoped_duplicate_count(self):
        scan = scanner()
        anchor = put(scan, 1, 100, "user", "Continue.", origin={"kind": "human"})
        scan.record(json.dumps(anchor).encode(), True, 2)
        put(scan, 3, 110, "assistant", [{"type": "tool_use", "id": "read", "name": "Read",
                                       "input": {"file_path": "/evidence/a"}}])
        put(scan, 4, 111, "user", [{"type": "tool_result", "tool_use_id": "read", "content": "ok"}])
        boundary = put(scan, 5, 120, "user", "Next.", origin={"kind": "human"})
        scan.record(json.dumps(boundary).encode(), True, 6)
        interval, reasons = parent_interval(scan)
        self.assertIsNotNone(interval)
        self.assertEqual(reasons, ())
        group = native.group_report(scan, interval)
        self.assertEqual(group["coverage"], "complete")
        self.assertEqual(group["counts"]["duplicate_records"], 1)
        self.assertEqual(group["counts"]["read_requests"], 1)
        self.assertEqual(len(scan.humans), 2)

    def test_conflicting_agent_identity_blocks_later_human_terminal_fallback(self):
        for agent_id in (None, "other", False, []):
            with self.subTest(agent_id=agent_id):
                scan = scanner()
                put(scan, 1, 100, "user", "Continue.", origin={"kind": "human"})
                put(scan, 2, 120, "user", "Next.", origin={"kind": "human"}, agentId=agent_id)
                interval, reasons = parent_interval(scan)
                self.assertIsNone(interval)
                self.assertEqual(reasons, ("interval_unbound",))
                self.assertIn("identity_conflict", scan.notes)

    def test_nonboolean_compaction_marker_is_neither_anchor_nor_skippable_boundary(self):
        for marker in ("true", "false", None, 0, 1, [], {}):
            for later in (False, True):
                with self.subTest(marker=marker, later=later):
                    scan = scanner()
                    if later:
                        put(scan, 1, 100, "user", "Continue.", origin={"kind": "human"})
                    value = put(scan, 2, 120 if later else 100, "user", "Next." if later else "Continue.",
                                origin={"kind": "human"}, isCompactSummary=marker)
                    self.assertFalse(native.human_candidate(value))
                    self.assertIsNone(parent_interval(scan)[0])
                    self.assertIn("source_malformed", scan.notes)

    def test_boolean_compaction_marker_preserves_genuine_and_summary_meanings(self):
        for marker in (False, True):
            scan = scanner()
            put(scan, 1, 100, "user", "Continue.", origin={"kind": "human"}, isCompactSummary=marker)
            self.assertEqual(parent_interval(scan)[0] is not None, marker is False)

    def test_attribution_and_missing_child_reasons_cannot_label_group_complete(self):
        for reason in ("owner_conflict", "child_attribution_ambiguous", "child_interval_unbound",
                       "child_missing", "launch_unresolved", "child_limit"):
            with self.subTest(reason=reason):
                scan = scanner()
                put(scan, 1, 100, "user", "Continue.", origin={"kind": "human"})
                scan.note(reason)
                group = native.group_report(scan, parent_interval(scan)[0])
                self.assertEqual(group["coverage"], "incomplete")
                self.assertIn("incomplete", [group[k] for k in ("source_coverage", "interval_coverage")])


class CollectorReviewTests(AuditFixture, unittest.TestCase):
    def test_earlier_rehash_failure_cannot_skip_proof_for_a_complete_child(self):
        self.write_transcript([
            self.human(),
            self.assistant_tools("launch", "2026-01-01T00:00:11+00:00",
                [{"type": "tool_use", "id": "t1", "name": "Agent", "input": {"prompt": "Read."}}]),
            self.user_results("completed", "2026-01-01T00:00:15+00:00",
                [{"type": "tool_result", "tool_use_id": "t1", "content": "done"}],
                toolUseResult=completed_result("a1", "Read."))])
        self.write_child("a1", [self.child_record("assistant", "child", "2026-01-01T00:00:12+00:00", "Done")])
        self.build()
        original = audit_io.rehash_source
        successful = []
        attempts = 0

        def rehash(opened, result, collector, limits):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                opened.close()
                raise audit_io.SourceError("source_changed")
            value = original(opened, result, collector, limits)
            successful.append(result.digest)
            return value

        with patch.object(audit_io, "rehash_source", rehash):
            report = self.audit_in_process()
        child = report["children"][0]
        if child["observation"]["source_coverage"] == "complete":
            child_source = next(source for source in report["sources"]
                                if source["actor_sha256"] == child["actor_sha256"])
            self.assertIn(child_source["source_sha256"], successful)
        self.assertEqual(report["coverage"], "incomplete")
        self.assertEqual(report["child_coverage"], "incomplete")

    def test_conflicting_error_and_success_never_opens_child_source(self):
        self.write_transcript([
            self.human(),
            self.assistant_tools("launch", "2026-01-01T00:00:11+00:00",
                [{"type": "tool_use", "id": "t1", "name": "Agent", "input": {"prompt": "Read."}}]),
            self.user_results("failed", "2026-01-01T00:00:12+00:00",
                [{"type": "tool_result", "tool_use_id": "t1", "is_error": True}]),
            self.user_results("completed", "2026-01-01T00:00:15+00:00",
                [{"type": "tool_result", "tool_use_id": "t1", "content": "done"}],
                toolUseResult=completed_result("a1", "Read."))])
        self.write_child("a1", [])
        self.build()
        original = audit_io.Root.open_file
        opened_children = []

        def opened(root, parts, *args, **kwargs):
            if parts[-1] == "agent-a1.jsonl":
                opened_children.append(parts)
            return original(root, parts, *args, **kwargs)

        with patch.object(audit_io.Root, "open_file", opened):
            report = self.audit_in_process()
        self.assertEqual(opened_children, [])
        self.assertEqual(report["children"], [])
        self.assertEqual(report["child_coverage"], "incomplete")

    def test_conflicting_later_human_does_not_attribute_following_read(self):
        later = self.human("human-2", "2026-01-01T00:00:20+00:00", "Next.")
        later["agentId"] = None
        self.write_transcript([
            self.human(), later,
            self.assistant_tools("read", "2026-01-01T00:00:25+00:00",
                [{"type": "tool_use", "id": "r1", "name": "Read", "input": {"file_path": self.target()}}]),
            self.user_results("result", "2026-01-01T00:00:26+00:00",
                [{"type": "tool_result", "tool_use_id": "r1", "content": "done"}])])
        self.build()
        report = self.audit_in_process()
        self.assertIsNone(report["parent"]["counts"]["read_requests"])
        self.assertIn("interval_unbound", report["reasons"])


if __name__ == "__main__":
    unittest.main()
