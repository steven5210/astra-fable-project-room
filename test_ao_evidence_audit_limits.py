"""Bound and bound+1 behaviour for the #58 audit, exercised on synthetic files through the module API."""

import unittest
from unittest import mock

import ao_evidence_audit_io
from test_ao_evidence_audit import AuditFixture, completed_result


class LimitBoundaryTests(AuditFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        target = self.target()
        self.records = [self.human(),
                        self.assistant_tools("u-read", "2026-01-01T00:00:11+00:00",
                                             [{"type": "tool_use", "id": "r1", "name": "Read",
                                               "input": {"file_path": target}}]),
                        self.user_results("u-result", "2026-01-01T00:00:12+00:00",
                                          [{"type": "tool_result", "tool_use_id": "r1", "content": "bounded result"}]),
                        self.human(uuid="human-2", timestamp="2026-01-01T00:01:00+00:00", text="Next.")]
        self.write_transcript(self.records)
        self.build()

    def report_with(self, name, value):
        with mock.patch.object(ao_evidence_audit_io, name, value):
            return self.audit_in_process()

    def test_transcript_byte_bound(self):
        size = self.transcript.stat().st_size
        self.assertEqual(self.report_with("MAX_TRANSCRIPT_BYTES", size)["coverage"], "complete")
        report = self.report_with("MAX_TRANSCRIPT_BYTES", size - 1)
        self.assertIn("transcript_bytes_limit", report["reasons"])
        self.assertNotEqual(report["coverage"], "complete")

    def test_record_byte_bound(self):
        payloads = self.transcript.read_text(encoding="utf-8").splitlines(keepends=True)
        bound = max(len(payload.rstrip("\n").encode("utf-8")) for payload in payloads)
        self.assertEqual(self.report_with("MAX_RECORD_BYTES", bound)["coverage"], "complete")
        report = self.report_with("MAX_RECORD_BYTES", bound - 1)
        self.assertIn("record_bytes_limit", report["reasons"])
        self.assertNotEqual(report["coverage"], "complete")

    def test_record_count_and_tool_id_bounds(self):
        self.assertEqual(self.report_with("MAX_RECORDS", len(self.records))["coverage"], "complete")
        report = self.report_with("MAX_RECORDS", len(self.records) - 1)
        self.assertIn("record_count_limit", report["reasons"])
        self.assertNotEqual(report["coverage"], "complete")
        self.assertEqual(self.report_with("MAX_TOOL_IDS", 1)["coverage"], "complete")
        second = self.assistant_tools("u-read-2", "2026-01-01T00:00:13+00:00",
                                      [{"type": "tool_use", "id": "r2", "name": "Read",
                                        "input": {"file_path": self.target("b.txt")}}])
        self.write_transcript([self.records[0], self.records[1], self.records[2], second, self.records[3]])
        report = self.report_with("MAX_TOOL_IDS", 1)
        self.assertIn("tool_id_limit", report["reasons"])
        self.assertNotEqual(report["coverage"], "complete")

    def test_child_actor_bound(self):
        self.write_transcript([self.human(),
                               self.assistant_tools("u-launches", "2026-01-01T00:00:11+00:00",
                                                    [{"type": "tool_use", "id": "t1", "name": "Agent",
                                                      "input": {"prompt": "Read bounded evidence."}},
                                                     {"type": "tool_use", "id": "t2", "name": "Agent",
                                                      "input": {"prompt": "Read bounded evidence."}}]),
                               self.user_results("u-result-1", "2026-01-01T00:00:12+00:00",
                                                 [{"type": "tool_result", "tool_use_id": "t1",
                                                   "content": "bounded result"}],
                                                 toolUseResult=completed_result("a1", "Read bounded evidence.")),
                               self.user_results("u-result-2", "2026-01-01T00:00:13+00:00",
                                                 [{"type": "tool_result", "tool_use_id": "t2",
                                                   "content": "bounded result"}],
                                                 toolUseResult=completed_result("a2", "Read bounded evidence.")),
                               self.human(uuid="human-2", timestamp="2026-01-01T00:01:00+00:00", text="Next.")])
        self.write_child("a1", [self.child_record("assistant", "c1-read", "2026-01-01T00:00:11.500000+00:00",
                                                  [{"type": "tool_use", "id": "k1", "name": "Read",
                                                    "input": {"file_path": self.target("a.txt")}}]),
                                self.child_record("user", "c1-result", "2026-01-01T00:00:11.750000+00:00",
                                                  [{"type": "tool_result", "tool_use_id": "k1",
                                                    "content": "bounded result"}])])
        self.write_child("a2", [self.child_record("assistant", "c2-read", "2026-01-01T00:00:12.500000+00:00",
                                                  [{"type": "tool_use", "id": "k2", "name": "Read",
                                                    "input": {"file_path": self.target("b.txt")}}], agent="a2"),
                                self.child_record("user", "c2-result", "2026-01-01T00:00:12.750000+00:00",
                                                  [{"type": "tool_result", "tool_use_id": "k2",
                                                    "content": "bounded result"}], agent="a2")])
        self.build()
        self.assertEqual(self.report_with("MAX_CHILD_ACTORS", 2)["child_coverage"], "complete")
        report = self.report_with("MAX_CHILD_ACTORS", 1)
        self.assertIn("child_limit", report["reasons"])
        self.assertNotEqual(report["child_coverage"], "complete")
        self.assertEqual(len(report["children"]), 1)

    def test_aggregate_byte_bound_applies_to_each_pass(self):
        target = self.target()
        self.write_child("a1", [self.child_record("assistant", "c-read", "2026-01-01T00:00:12+00:00",
                                                  [{"type": "tool_use", "id": "k1", "name": "Read",
                                                    "input": {"file_path": target}}]),
                                self.child_record("user", "c-result", "2026-01-01T00:00:13+00:00",
                                                  [{"type": "tool_result", "tool_use_id": "k1",
                                                    "content": "bounded result"}])])
        self.write_transcript([self.human(),
                               self.assistant_tools("u-launch", "2026-01-01T00:00:11+00:00",
                                                    [{"type": "tool_use", "id": "t1", "name": "Agent",
                                                      "input": {"prompt": "Read bounded evidence."}}]),
                               self.user_results("u-launch-result", "2026-01-01T00:00:15+00:00",
                                                 [{"type": "tool_result", "tool_use_id": "t1",
                                                   "content": "bounded result"}],
                                                 toolUseResult=completed_result("a1", "Read bounded evidence.")),
                               self.human(uuid="human-2", timestamp="2026-01-01T00:01:00+00:00", text="Next.")])
        self.build()
        total = self.transcript.stat().st_size + (self.subagents / "agent-a1.jsonl").stat().st_size
        self.assertEqual(self.report_with("MAX_AGGREGATE_BYTES", total)["coverage"], "complete")
        report = self.report_with("MAX_AGGREGATE_BYTES", total - 1)
        self.assertIn("aggregate_bytes_limit", report["reasons"])
        self.assertNotEqual(report["coverage"], "complete")

    def test_directory_entry_and_binding_byte_bounds(self):
        self.assertEqual(self.report_with("MAX_DIRECTORY_ENTRIES", 1)["coverage"], "complete")
        report = self.report_with("MAX_DIRECTORY_ENTRIES", 0)
        self.assertIn("directory_entry_limit", report["reasons"])
        self.assertNotEqual(report["coverage"], "complete")
        room = self.home / "ao" / "rooms" / self.room
        receipt_size = (room / self.request["receipt"]).stat().st_size
        bound = max(receipt_size, (room / "state.json").stat().st_size,
                    (room / "preparation.json").stat().st_size)
        self.assertEqual(bound, receipt_size)
        self.assertEqual(self.report_with("MAX_BINDING_BYTES", bound)["coverage"], "complete")
        report = self.report_with("MAX_BINDING_BYTES", bound - 1)
        self.assertIn("binding_bytes_limit", report["reasons"])
        self.assertNotEqual(report["coverage"], "complete")

    def test_json_depth_and_path_byte_bounds(self):
        depth = max(self.record_depth(item) for item in self.records)
        self.assertEqual(self.report_with("MAX_JSON_DEPTH", depth)["coverage"], "complete")
        report = self.report_with("MAX_JSON_DEPTH", depth - 1)
        self.assertIn("json_depth_limit", report["reasons"])
        self.assertNotEqual(report["coverage"], "complete")
        # This lexical target is deliberately longer than all real binding/source paths;
        # a smaller global path cap must exercise Read classification, not earlier binding refusal.
        target = self.target("x" * 1000)
        for record in self.records:
            content = record["message"]["content"]
            if isinstance(content, list):
                for block in content:
                    if block.get("type") == "tool_use" and block.get("name") == "Read":
                        block["input"]["file_path"] = target
        self.write_transcript(self.records)
        size = len(target.encode("utf-8"))
        self.assertEqual(self.report_with("MAX_PATH_BYTES", size)["coverage"], "complete")
        report = self.report_with("MAX_PATH_BYTES", size - 1)
        self.assertIn("path_bytes_limit", report["reasons"])
        self.assertEqual(report["parent"]["counts"]["unclassified_paths"], 1)

    @staticmethod
    def record_depth(value):
        if isinstance(value, dict):
            return 1 + max([LimitBoundaryTests.record_depth(item) for item in value.values()] or [0])
        if isinstance(value, list):
            return 1 + max([LimitBoundaryTests.record_depth(item) for item in value] or [0])
        return 0


if __name__ == "__main__":
    unittest.main()
