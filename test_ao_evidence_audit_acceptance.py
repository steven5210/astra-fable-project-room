"""Synthetic counterexamples for the three rejected #58 acceptance contracts.

These exercise the actual standalone CLI, scanner and collector without any account,
provider, external evidence target or production room access.
"""
import json
import unittest
from unittest import mock

import ao_evidence_audit_io as fs
import ao_evidence_audit_native as native
from test_ao_evidence_audit import AuditFixture, canon, completed_result
from test_ao_evidence_audit_independent import put, scanner


class EndpointTests(unittest.TestCase):
    def test_exclusive_and_inclusive_endpoints_reach_every_descendant(self):
        for kind, included in (("boundary", False), ("terminal", True)):
            with self.subTest(kind=kind):
                interval = native.Interval(kind, 1, 10, 99 if kind == "boundary" else None, 20)
                child = interval.child(11, 20)
                grandchild = child.child(12, 20)
                for scope in (interval, child, grandchild):
                    self.assertEqual(scope.contains(5, 20), included)
                    self.assertEqual(scope.contains_time(20), included)
                    self.assertEqual(scope.covers_child(4, 19, 5, 20), included)
                    self.assertTrue(scope.contains_time(19.999))
                    self.assertFalse(scope.contains_time(20.001))

    def test_human_boundary_still_requires_actor_local_record_order(self):
        interval = native.Interval("boundary", 5, 10, 9, 20)
        self.assertFalse(interval.contains(5, 11))
        self.assertTrue(interval.contains(6, 11))
        self.assertFalse(interval.contains(9, 19))
        self.assertFalse(interval.contains(10, 19))


class ToolIdentityTests(unittest.TestCase):
    @staticmethod
    def result(tool_id, **extra):
        return {"type": "tool_result", "tool_use_id": tool_id, "content": "ok", **extra}

    @staticmethod
    def use(tool_id):
        return {"type": "tool_use", "id": tool_id, "name": "Read", "input": {"file_path": "/evidence/a"}}

    def test_orphan_result_is_charged_before_retention(self):
        scan = scanner()
        scan.collector.limits["tool_ids"] = 1
        put(scan, 1, 110, "user", [self.result("first")])
        self.assertEqual(scan.collector.tool_ids, 1)
        with self.assertRaises(fs.SourceError) as caught:
            put(scan, 2, 111, "user", [self.result("second")])
        self.assertEqual(caught.exception.reason, "tool_id_limit")
        self.assertEqual(set(scan.results), {"first"})
        self.assertEqual(scan.tool_entries, {})

    def test_result_and_use_union_charges_once_in_both_orders(self):
        for result_first in (False, True):
            with self.subTest(result_first=result_first):
                scan = scanner()
                scan.collector.limits["tool_ids"] = 1
                records = [("assistant", [self.use("same")]), ("user", [self.result("same")])]
                if result_first:
                    records.reverse()
                for number, (role, content) in enumerate(records * 2, 1):
                    put(scan, number, 110 + number, role, content)
                self.assertEqual(scan.collector.tool_ids, 1)
                self.assertEqual(set(scan.tool_entries) | set(scan.results), {"same"})

    def test_same_result_id_in_different_actors_charges_twice(self):
        first = scanner()
        first.collector.limits["tool_ids"] = 1
        second = native.RecordScanner(first.collector, "b" * 64, "parent", None,
                                      "native-session", "/workspace", "/evidence")
        put(first, 1, 110, "user", [self.result("same")])
        with self.assertRaises(fs.SourceError) as caught:
            put(second, 1, 110, "user", [self.result("same")])
        self.assertEqual(caught.exception.reason, "tool_id_limit")
        self.assertEqual(second.results, {})

    def test_result_limit_inside_one_record_keeps_only_bounded_prefix(self):
        scan = scanner()
        scan.collector.limits["tool_ids"] = 2
        with self.assertRaises(fs.SourceError) as caught:
            put(scan, 1, 110, "user", [self.result("one"), self.result("two"), self.result("three")])
        self.assertEqual(caught.exception.reason, "tool_id_limit")
        self.assertEqual(set(scan.results), {"one", "two"})
        self.assertNotIn("three", scan.tool_entries)

    def test_malformed_result_with_valid_identity_still_consumes_a_retained_id(self):
        scan = scanner()
        scan.collector.limits["tool_ids"] = 1
        put(scan, 1, 110, "user", [self.result("bad", is_error=None)])
        self.assertEqual(scan.collector.tool_ids, 1)
        with self.assertRaises(fs.SourceError):
            put(scan, 2, 111, "assistant", [self.use("new")])
        self.assertNotIn("new", scan.tool_entries)

    def test_tool_use_refusal_retains_no_empty_entry(self):
        scan = scanner()
        scan.collector.limits["tool_ids"] = 1
        put(scan, 1, 110, "assistant", [self.use("one")])
        with self.assertRaises(fs.SourceError):
            put(scan, 2, 111, "assistant", [self.use("two")])
        self.assertEqual(set(scan.tool_entries), {"one"})


class CollectorAcceptanceTests(AuditFixture, unittest.TestCase):
    @staticmethod
    def stamp(second):
        return "2026-01-01T00:00:%02d+00:00" % second

    def record(self, owner, kind, uuid, second, content, **extra):
        if owner is None:
            return self.parent_record(kind, uuid, self.stamp(second), content, **extra)
        return self.child_record(kind, uuid, self.stamp(second), content, agent=owner, **extra)

    def launch(self, owner, target, start=11, end=35, uuid=None):
        uuid = uuid or "launch-" + target
        prompt = "Inspect " + target
        tool_id = "tool-" + uuid
        return [self.record(owner, "assistant", uuid, start,
                    [{"type": "tool_use", "id": tool_id, "name": "Agent", "input": {"prompt": prompt}}]),
                self.record(owner, "user", uuid + "-result", end,
                    [{"type": "tool_result", "tool_use_id": tool_id, "content": "Done."}],
                    toolUseResult=completed_result(target, prompt))]

    def read(self, owner, start=20, end=21, source_uuid=None, pointer_on="user"):
        values = [self.record(owner, "assistant", "read-use", start,
                      [{"type": "tool_use", "id": "r", "name": "Read", "input": {"file_path": self.target()}}]),
                  self.record(owner, "user", "read-result", end,
                      [{"type": "tool_result", "tool_use_id": "r", "content": "Reported bytes."}])]
        if source_uuid is not None:
            values[0 if pointer_on == "assistant" else 1]["sourceToolAssistantUUID"] = source_uuid
        return values

    def collect(self):
        opened, original = [], fs._open_component
        def observe(name, *args):
            if name.startswith("agent-"):
                opened.append(name)
            return original(name, *args)
        with mock.patch.object(fs, "_open_component", side_effect=observe):
            report = self.audit_in_process()
        actors = {entry["actor_sha256"]: entry["observation"] for entry in report["children"]}
        def key(actor):
            return canon({"owner_sha256": report["owner_sha256"], "kind": "child", "agent_id": actor})
        return report, opened, actors, key

    def test_cli_read_and_result_at_next_human_timestamp_are_excluded(self):
        self.build()
        self.write_transcript([self.human()] + self.read(None, 20, 20)
                              + [self.human("next", self.stamp(20), "Next request.")])
        report = self.report()
        self.assertEqual(report["parent"]["counts"]["read_requests"], 0)
        self.assertEqual(report["parent"]["counts"]["reported_non_error_results"], 0)
        self.assertEqual(report["coverage"], "complete")

    def test_boundary_result_cannot_complete_an_earlier_read(self):
        self.build()
        self.write_transcript([self.human()] + self.read(None, 19, 20)
                              + [self.human("next", self.stamp(20), "Next request.")])
        report = self.audit_in_process()
        self.assertEqual(report["parent"]["counts"]["read_requests"], 1)
        self.assertEqual(report["parent"]["counts"]["reported_non_error_results"], 0)
        self.assertEqual(report["parent"]["counts"]["unresolved_read_results"], 1)

    def test_child_completion_at_next_human_timestamp_opens_no_child(self):
        for start in (19, 20):
            with self.subTest(start=start):
                self.reset()
                self.build()
                self.write_transcript([self.human()] + self.launch(None, "a1", start, 20)
                                      + [self.human("next", self.stamp(20), "Next request.")])
                self.write_child("a1", self.read("a1", 20, 20))
                report, opened, actors, key = self.collect()
                self.assertNotIn("agent-a1.jsonl", opened)
                self.assertEqual(actors, {})

    def test_terminal_equal_timestamp_preserves_parent_and_recursive_children(self):
        self.build(completed_at=self.stamp(20))
        self.write_transcript([self.human()] + self.read(None, 20, 20) + self.launch(None, "a1", 20, 20))
        self.write_child("a1", self.read("a1", 20, 20) + self.launch("a1", "b1", 20, 20))
        self.write_child("b1", self.read("b1", 20, 20))
        report = self.report()
        self.assertEqual(report["coverage"], "complete")
        self.assertEqual(report["parent"]["counts"]["read_requests"], 1)
        self.assertEqual(len(report["children"]), 2)
        self.assertTrue(all(item["observation"]["counts"]["read_requests"] == 1 for item in report["children"]))

    def child_pointer_fixture(self, source_uuid, pointer_on="user", outside=False):
        self.build()
        prior = self.launch(None, "a2", 5, 6) if outside else []
        later = [] if outside else self.launch(None, "a2")
        self.write_transcript(prior + [self.human()] + self.launch(None, "a1") + later)
        self.write_child("a1", self.read("a1", source_uuid=source_uuid, pointer_on=pointer_on)
                         + self.launch("a1", "b1", 22, 30))
        self.write_child("a2", [self.record("a2", "assistant", "prose", 20, "Done.")])
        self.write_child("b1", self.read("b1", 23, 24))

    def test_cli_child_known_parent_launch_contradiction_is_null(self):
        self.child_pointer_fixture("launch-a2")
        report = self.report()
        a1 = canon({"owner_sha256": report["owner_sha256"], "kind": "child", "agent_id": "a1"})
        observation = next(item["observation"] for item in report["children"] if item["actor_sha256"] == a1)
        self.assertIsNone(observation)
        self.assertIn("child_attribution_ambiguous", report["reasons"])

    def test_child_user_and_assistant_parent_pointer_contradictions_remove_path_authority(self):
        for pointer_on in ("user", "assistant"):
            for outside in (False, True):
                with self.subTest(pointer_on=pointer_on, outside=outside):
                    self.reset()
                    self.child_pointer_fixture("launch-a2", pointer_on, outside)
                    report, opened, actors, key = self.collect()
                    self.assertIsNone(actors[key("a1")])
                    self.assertNotIn("agent-b1.jsonl", opened)
                    self.assertNotIn(key("b1"), actors)
                    self.assertIn("child_attribution_ambiguous", report["reasons"])

    def test_matching_or_child_owned_pointer_preserves_authority(self):
        for pointer_on in ("user", "assistant"):
            for source_uuid in (None, "launch-a1", "child-owned-uuid"):
                with self.subTest(pointer_on=pointer_on, source_uuid=source_uuid):
                    self.reset()
                    self.child_pointer_fixture(source_uuid, pointer_on)
                    report, opened, actors, key = self.collect()
                    self.assertEqual(report["coverage"], "complete")
                    self.assertEqual(actors[key("a1")]["counts"]["read_requests"], 1)
                    self.assertIn("agent-b1.jsonl", opened)

    def test_recursive_pointer_uses_immediate_parent_launch_namespace(self):
        for source_uuid, allowed in (("launch-b2", False), ("launch-b1", True), ("launch-a2", True)):
            with self.subTest(source_uuid=source_uuid):
                self.reset()
                self.build()
                self.write_transcript([self.human()] + self.launch(None, "a1") + self.launch(None, "a2"))
                self.write_child("a1", self.launch("a1", "b1", 15, 30) + self.launch("a1", "b2", 15, 30))
                self.write_child("a2", [self.record("a2", "assistant", "prose", 20, "Done.")])
                self.write_child("b1", self.read("b1", source_uuid=source_uuid)
                                 + self.launch("b1", "c1", 22, 25))
                self.write_child("b2", [self.record("b2", "assistant", "prose", 20, "Done.")])
                self.write_child("c1", self.read("c1", 23, 24))
                report, opened, actors, key = self.collect()
                if allowed:
                    self.assertEqual(report["coverage"], "complete")
                    self.assertIn("agent-c1.jsonl", opened)
                else:
                    self.assertIsNone(actors[key("b1")])
                    self.assertNotIn("agent-c1.jsonl", opened)

    def test_pointer_revocation_preserves_validated_negative_claims(self):
        self.child_pointer_fixture("launch-a2")
        self.write_child("a1", self.read("a1", source_uuid="launch-a2")
                         + self.launch("a1", "a2", 22, 30) + self.launch("a1", "b1", 22, 30))
        report, opened, actors, key = self.collect()
        self.assertIsNone(actors[key("a1")])
        self.assertIsNone(actors[key("a2")])
        self.assertNotIn("agent-b1.jsonl", opened)

    def test_cli_orphan_results_obey_actual_50000_identity_limit(self):
        for total, allowed in ((50000, True), (50001, False)):
            with self.subTest(total=total):
                self.reset()
                self.build()
                records = [self.human()] + self.read(None)
                records[-1]["message"]["content"].extend(
                    {"type": "tool_result", "tool_use_id": "o%d" % index}
                    for index in range(total - 1))
                self.write_transcript(records)
                self.assertLess(self.transcript.stat().st_size, fs.MAX_TRANSCRIPT_BYTES)
                self.assertLess(max(map(len, self.transcript.read_bytes().splitlines())), fs.MAX_RECORD_BYTES)
                report = self.report()
                if allowed:
                    self.assertEqual(report["coverage"], "complete")
                    self.assertEqual(report["parent"]["counts"]["read_requests"], 1)
                else:
                    self.assertIn("tool_id_limit", report["reasons"])
                    self.assertNotEqual(report["coverage"], "complete")
                    self.assertEqual(report["sources"], [])

    def test_result_limit_salvages_reuse_and_closes_global_positive_admission(self):
        self.build()
        self.write_transcript([self.human()] + self.launch(None, "a5") + self.launch(None, "a1")
                              + self.launch(None, "a7"))
        self.write_child("a5", [self.record("a5", "assistant", "prose", 20, "Done.")])
        self.write_child("a1", self.launch("a1", "a5", 20, 25)
                         + [self.record("a1", "user", "orphans", 26,
                             [{"type": "tool_result", "tool_use_id": "orphan-1"},
                              {"type": "tool_result", "tool_use_id": "orphan-2"}])])
        self.write_child("a7", [self.record("a7", "assistant", "prose", 20, "Done.")])
        with mock.patch.object(fs, "MAX_TOOL_IDS", 5):
            report, opened, actors, key = self.collect()
        self.assertIsNone(actors[key("a1")])
        self.assertIsNone(actors[key("a5")])
        self.assertNotIn("agent-a7.jsonl", opened)
        self.assertEqual(opened.count("agent-a1.jsonl"), 1)
        self.assertIn("tool_id_limit", report["reasons"])
        self.assertNotIn(key("a1"), {entry["actor_sha256"] for entry in report["sources"]})


if __name__ == "__main__":
    unittest.main()
