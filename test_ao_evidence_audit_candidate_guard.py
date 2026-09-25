"""F58-N1: an actor losing authority mid-expansion must stop authorizing new paths.

All records and files are synthetic. Hooks observe actual descriptor opens; candidate
generation and collection use the real implementation and structured completion profile.
"""

import unittest
from unittest import mock

import ao_evidence_audit_io as fs
from test_ao_evidence_audit import AuditFixture, completed_result


class CandidateGuardTests(AuditFixture, unittest.TestCase):
    def fixture(self, order):
        self.build()
        self.write_transcript([
            self.human(),
            self.assistant_tools("parent-launch", "2026-01-01T00:00:20+00:00", [
                {"type": "tool_use", "id": "parent-agent", "name": "Agent", "input": {"prompt": "inspect"}}]),
            self.user_results("parent-completed", "2026-01-01T00:00:35+00:00", [
                {"type": "tool_result", "tool_use_id": "parent-agent", "content": "Finished."}],
                toolUseResult=completed_result("a1", "inspect"))])
        records = []
        for index, kind in enumerate(order):
            start = 22 + 2 * index
            agent = "a1" if kind == "reuse" else "a2"
            records.extend([
                self.child_record("assistant", kind + "-launch", "2026-01-01T00:00:%02d+00:00" % start, [
                    {"type": "tool_use", "id": kind + "-tool", "name": "Agent", "input": {"prompt": kind}}]),
                self.child_record("user", kind + "-completed", "2026-01-01T00:00:%02d+00:00" % (start + 1), [
                    {"type": "tool_result", "tool_use_id": kind + "-tool", "content": "Finished."}],
                    toolUseResult=completed_result(agent, kind))])
        self.write_child("a1", records)
        later_start = 22 + 2 * order.index("later")
        self.write_child("a2", [self.child_record("assistant", "a2-prose",
            "2026-01-01T00:00:%02d+00:00" % later_start, "Synthetic child prose.", agent="a2")])

    def collect_open_names(self):
        original, opened = fs._open_component, []
        def observe(name, *args):
            opened.append(name)
            return original(name, *args)
        with mock.patch.object(fs, "_open_component", side_effect=observe):
            report = self.audit_in_process()
        return report, opened

    def test_self_reuse_stops_later_candidate_in_same_scan(self):
        self.fixture(("reuse", "later"))
        report, opened = self.collect_open_names()
        self.assertNotIn("agent-a2.jsonl", opened)
        self.assertIn("agent-a1.jsonl", opened)
        self.assertEqual(len(report["children"]), 1)
        self.assertIsNone(report["children"][0]["observation"])
        self.assertEqual(len(report["sources"]), 2)
        self.assertEqual(report["child_coverage"], "incomplete")
        self.assertEqual(report["coverage"], "incomplete")
        self.assertIn("child_attribution_ambiguous", report["reasons"])

    def test_valid_nested_candidate_is_still_opened_without_reuse(self):
        self.fixture(("later",))
        report, opened = self.collect_open_names()
        self.assertIn("agent-a2.jsonl", opened)
        self.assertEqual(len(report["children"]), 2)
        self.assertEqual(len(report["sources"]), 3)
        self.assertEqual(report["child_coverage"], "complete")
        self.assertEqual(report["coverage"], "complete")

    def test_candidate_admitted_before_reuse_retains_only_ambiguous_evidence(self):
        self.fixture(("later", "reuse"))
        report, opened = self.collect_open_names()
        self.assertIn("agent-a2.jsonl", opened)
        self.assertEqual(len(report["children"]), 2)
        self.assertTrue(all(child["observation"] is None for child in report["children"]))
        self.assertEqual(len(report["sources"]), 3)
        self.assertEqual(report["child_coverage"], "incomplete")
        self.assertEqual(report["coverage"], "incomplete")
        self.assertIn("child_attribution_ambiguous", report["reasons"])


if __name__ == "__main__":
    unittest.main()
