"""F58-N1: an actor losing authority mid-expansion must stop authorizing new paths.

All records and files are synthetic. Hooks observe actual descriptor opens; candidate
generation and collection use the real implementation and structured completion profile.
"""

import json
import unittest
from unittest import mock

import ao_evidence_audit_io as fs
import ao_evidence_audit_native as native
from test_ao_evidence_audit import AuditFixture, NATIVE_UUID, canon, completed_result


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


class NegativeClaimTests(AuditFixture, unittest.TestCase):
    """Negative uniqueness evidence survives revocation; it never grants a new path."""

    def claims(self, owner, targets):
        records = []
        start, end = ("11", "38") if owner is None else ("14", "30")
        for index, target in enumerate(targets):
            key = "claim-%d" % index
            use = [{"type": "tool_use", "id": key, "name": "Agent", "input": {"prompt": key}}]
            result = [{"type": "tool_result", "tool_use_id": key, "content": "Finished."}]
            completed = completed_result(target, key)
            if owner is None:
                records.extend([
                    self.assistant_tools(key + "-use", "2026-01-01T00:00:" + start + "+00:00", use),
                    self.user_results(key + "-result", "2026-01-01T00:00:" + end + "+00:00", result,
                                      toolUseResult=completed)])
            else:
                records.extend([
                    self.child_record("assistant", key + "-use", "2026-01-01T00:00:" + start + "+00:00",
                                      use, agent=owner),
                    self.child_record("user", key + "-result", "2026-01-01T00:00:" + end + "+00:00",
                                      result, agent=owner, toolUseResult=completed)])
        return records

    def graph(self, roots, edges, *, contradictory=(), erroneous=(), conflicting=()):
        self.build()
        self.write_transcript([self.human()] + self.claims(None, roots))
        actors = set(roots) | set(edges) | {target for targets in edges.values() for target in targets}
        for actor in sorted(actors):
            records = self.claims(actor, edges.get(actor, ()))
            if actor in erroneous:
                records[1]["message"]["content"][0]["is_error"] = True
            if actor in conflicting:
                records.append({**records[1], "uuid": "conflicting-result",
                                "toolUseResult": completed_result("unproved-id", "claim-0")})
            if actor in contradictory:
                records.append(self.child_record("assistant", "wrong-owner", "2026-01-01T00:00:20+00:00",
                                                 "Synthetic conflicting owner.", agent="different-owner"))
            if not records:
                records = [self.child_record("assistant", "prose", "2026-01-01T00:00:20+00:00",
                                             "Synthetic child prose.", agent=actor)]
            self.write_child(actor, records)

    def collect(self):
        original, opened = fs._open_component, []
        original_scan, original_rehash = fs.scan_source, fs.rehash_source
        self.first_pass, self.second_pass = [], []
        def observe(name, *args):
            if name.startswith("agent-") and name.endswith(".jsonl"):
                opened.append(name)
            return original(name, *args)
        def scan(opened, *args):
            self.first_pass.append(opened.name)
            return original_scan(opened, *args)
        def rehash(opened, *args):
            self.second_pass.append(opened.name)
            return original_rehash(opened, *args)
        with mock.patch.object(fs, "_open_component", side_effect=observe), \
             mock.patch.object(fs, "scan_source", side_effect=scan), \
             mock.patch.object(fs, "rehash_source", side_effect=rehash):
            report = self.audit_in_process()
        actors = {item["actor_sha256"]: item["observation"] for item in report["children"]}
        def actor_key(agent):
            return canon({"owner_sha256": report["owner_sha256"], "kind": "child", "agent_id": agent})
        return report, opened, actors, actor_key

    def test_revoked_scan_still_marks_unrelated_reuse_in_either_order(self):
        for revoked in ("a1", "a2"):
            for order in ((revoked, "a5", "a9"), ("a5", revoked, "a9")):
                with self.subTest(revoked=revoked, order=order):
                    self.reset()
                    self.graph(("a1", "a5"), {"a1": ("a2",), "a2": order})
                    report, opened, actors, key = self.collect()
                    self.assertIsNone(actors[key("a5")])
                    self.assertIsNone(actors[key("a2")])
                    self.assertNotIn("agent-a9.jsonl", opened)
                    self.assertNotIn(key("a9"), actors)
                    self.assertEqual(report["child_coverage"], "incomplete")
                    self.assertIn("child_attribution_ambiguous", report["reasons"])

    def test_already_revoked_queued_source_still_registers_negative_claims(self):
        self.graph(("a1", "a1", "a5"), {"a1": ("a5", "a9")})
        report, opened, actors, key = self.collect()
        self.assertIsNone(actors[key("a1")])
        self.assertIsNone(actors[key("a5")])
        self.assertNotIn("agent-a9.jsonl", opened)
        self.assertEqual(len(report["sources"]), 3)

    def test_queued_descendant_revoked_before_dequeue_still_registers_claims(self):
        self.graph(("a1", "a5"), {"a1": ("a2", "a1"), "a2": ("a5", "a9")})
        report, opened, actors, key = self.collect()
        self.assertIsNone(actors[key("a5")])
        self.assertIsNone(actors[key("a2")])
        self.assertNotIn("agent-a9.jsonl", opened)
        self.assertIn("agent-a2.jsonl", self.first_pass)
        self.assertIn("agent-a2.jsonl", self.second_pass)
        self.assertEqual(len(report["sources"]), 4)

    def test_contradictory_source_only_registers_validated_negative_claims(self):
        self.graph(("a1", "a5"), {"a1": ("a5", "a9")}, contradictory=("a1",))
        report, opened, actors, key = self.collect()
        self.assertIsNone(actors[key("a1")])
        self.assertIsNone(actors[key("a5")])
        self.assertNotIn("agent-a9.jsonl", opened)
        self.assertEqual(report["child_coverage"], "incomplete")

    def test_skipped_new_id_cannot_become_unique_in_a_later_actor(self):
        for roots in (("a1", "a5"), ("a1", "a1", "a5")):
            with self.subTest(roots=roots):
                self.reset()
                self.graph(roots, {"a1": ("a1", "a9"), "a5": ("a9",)})
                report, opened, actors, key = self.collect()
                self.assertNotIn("agent-a9.jsonl", opened)
                self.assertNotIn(key("a9"), actors)
                self.assertEqual(len(report["sources"]), 3)
                self.assertEqual(report["child_coverage"], "incomplete")

    def test_earlier_admitted_id_is_revoked_by_a_later_blocked_claim(self):
        self.graph(("a5", "a1"), {"a1": ("a1", "a9"), "a5": ("a9",)})
        report, opened, actors, key = self.collect()
        self.assertIn("agent-a9.jsonl", opened)
        self.assertIsNone(actors[key("a9")])
        self.assertEqual(len(report["sources"]), 4)
        self.assertEqual(report["child_coverage"], "incomplete")

    def test_later_negative_claim_nulls_already_read_descendants_without_expansion(self):
        self.graph(("a5", "a1"), {"a5": ("a9",), "a1": ("a2",), "a9": ("a10",),
                                    "a2": ("a1", "a9"), "a10": ("a11",)})
        report, opened, actors, key = self.collect()
        for actor in ("a9", "a10"):
            self.assertIsNone(actors[key(actor)])
            self.assertIn("agent-" + actor + ".jsonl", self.first_pass)
            self.assertIn("agent-" + actor + ".jsonl", self.second_pass)
        self.assertNotIn("agent-a11.jsonl", opened)
        self.assertEqual(len(report["sources"]), 6)

    def test_unrelated_actor_revocation_does_not_block_new_local_authority(self):
        self.graph(("a1", "a5"), {"a1": ("a2",), "a2": ("a5", "a9")})
        report, opened, actors, key = self.collect()
        self.assertIsNone(actors[key("a5")])
        self.assertIn("agent-a9.jsonl", opened)
        self.assertEqual(actors[key("a9")]["coverage"], "complete")
        self.assertEqual(report["child_coverage"], "incomplete")

    def test_actor_cutoff_stops_new_sources_but_not_existing_negative_evidence(self):
        roots = tuple("a%d" % index for index in range(33)) + ("a1",)
        self.graph(roots, {"a2": ("a3", "new-id")})
        report, opened, actors, key = self.collect()
        self.assertIsNone(actors[key("a1")])
        self.assertIsNone(actors[key("a3")])
        self.assertNotIn("agent-a32.jsonl", opened)
        self.assertNotIn("agent-new-id.jsonl", opened)
        self.assertEqual(len(actors), 32)
        self.assertEqual(len(report["sources"]), 33)
        self.assertEqual(len(self.first_pass), 33)
        self.assertEqual(len(self.second_pass), 33)
        self.assertIn("child_limit", report["reasons"])

    def test_exact_actor_limit_with_only_revoked_novel_claims_is_not_over_bound(self):
        roots = tuple("a%d" % index for index in range(32)) + ("a0",)
        self.graph(roots, {"a0": ("new-id", "another-id")})
        report, opened, actors, key = self.collect()
        self.assertNotIn("agent-new-id.jsonl", opened)
        self.assertNotIn("agent-another-id.jsonl", opened)
        self.assertEqual(len(actors), 32)
        self.assertEqual(len(report["sources"]), 33)
        self.assertNotIn("child_limit", report["reasons"])

    def test_negative_claim_registry_uses_tool_bound_not_actor_admission_bound(self):
        skipped = tuple("blocked%d" % index for index in range(40))
        self.graph(("a1", "a5"), {"a1": ("a1",) + skipped, "a5": (skipped[-1],)})
        report, opened, actors, key = self.collect()
        self.assertNotIn("agent-" + skipped[-1] + ".jsonl", opened)
        self.assertEqual(len(actors), 2)
        self.assertNotIn("child_limit", report["reasons"])
        self.assertEqual(len(report["sources"]), 3)

    def test_invalid_results_do_not_invent_a_negative_actor_id(self):
        for shape in ("error", "conflicting"):
            with self.subTest(shape=shape):
                self.reset()
                self.graph(("a1", "a5"), {"a1": ("a9",), "a5": ("a9",)},
                           erroneous=("a1",) if shape == "error" else (),
                           conflicting=("a1",) if shape == "conflicting" else ())
                report, opened, actors, key = self.collect()
                self.assertIn("agent-a9.jsonl", opened)
                self.assertIn("agent-a9.jsonl", self.first_pass)
                self.assertEqual(actors[key("a9")]["coverage"], "complete")
                self.assertNotIn("agent-unproved-id.jsonl", opened)
                self.assertEqual(report["child_coverage"], "incomplete")

    def failed_scan_assertions(self, report, opened, actors, key, reason):
        self.assertIsNone(actors[key("a1")])
        self.assertIsNone(actors[key("a5")])
        self.assertNotIn("agent-a9.jsonl", opened)
        self.assertNotIn(key("a9"), actors)
        self.assertEqual(opened.count("agent-a1.jsonl"), 1)
        self.assertIn("agent-a1.jsonl", self.first_pass)
        self.assertNotIn("agent-a1.jsonl", self.second_pass)
        self.assertNotIn(key("a1"), {item["actor_sha256"] for item in report["sources"]})
        self.assertIn(reason, report["reasons"])
        self.assertIn("child_attribution_ambiguous", report["reasons"])
        self.assertEqual(report["child_coverage"], "incomplete")

    def append_large_record(self, actor):
        path = self.subagents / ("agent-" + actor + ".jsonl")
        with path.open("ab") as handle:
            handle.write(b'"' + b"x" * 20000 + b'"\n')

    def test_record_byte_failure_keeps_valid_prefix_negative_only(self):
        self.graph(("a5", "a1", "a7"), {"a1": ("a5", "a9")})
        self.append_large_record("a1")
        with mock.patch.object(fs, "MAX_RECORD_BYTES", 2000):
            report, opened, actors, key = self.collect()
        self.failed_scan_assertions(report, opened, actors, key, "record_bytes_limit")
        self.assertIn("agent-a7.jsonl", opened)
        self.assertEqual(actors[key("a7")]["coverage"], "complete")

    def test_global_failures_keep_prefix_claims_and_stop_further_admission(self):
        for constant, bound, reason in (("MAX_RECORDS", 12, "record_count_limit"),
                                        ("MAX_RECORD_IDS", 12, "record_id_limit"),
                                        ("MAX_TOOL_IDS", 5, "tool_id_limit")):
            with self.subTest(constant=constant):
                self.reset()
                self.graph(("a5", "a1", "a7"), {"a1": ("a5", "a9", "unread-id")})
                # Parent: seven records / three tools; a5: one prose record;
                # a1: two complete claims, then the first over-limit record/tool.
                with mock.patch.object(fs, constant, bound):
                    report, opened, actors, key = self.collect()
                self.failed_scan_assertions(report, opened, actors, key, reason)
                self.assertNotIn("agent-a7.jsonl", opened)
                self.assertNotIn("agent-unread-id.jsonl", opened)
                self.assertNotIn(key("a7"), actors)
                self.assertNotIn(key("unread-id"), actors)
                self.assertEqual(len(report["sources"]), 2)
                self.assertEqual(len(actors), 2)

    def test_failed_prefix_novel_claim_blocks_later_competing_authority(self):
        self.graph(("a5", "a1", "a7"), {"a1": ("a5", "a9"), "a7": ("a9",)})
        self.append_large_record("a1")
        with mock.patch.object(fs, "MAX_RECORD_BYTES", 2000):
            report, opened, actors, key = self.collect()
        self.failed_scan_assertions(report, opened, actors, key, "record_bytes_limit")
        self.assertIn("agent-a7.jsonl", opened)
        self.assertEqual(len(report["sources"]), 3)

    def test_failure_before_completed_result_cannot_invent_agent_id(self):
        self.graph(("a1", "a7"), {"a1": ("a9",), "a7": ("a9",)})
        self.write_child("a1", self.claims("a1", ("a9",))[:1])
        self.append_large_record("a1")
        with mock.patch.object(fs, "MAX_RECORD_BYTES", 2000):
            report, opened, actors, key = self.collect()
        self.assertIsNone(actors[key("a1")])
        self.assertIn("agent-a9.jsonl", opened)
        self.assertEqual(actors[key("a9")]["coverage"], "complete")
        self.assertNotIn("agent-a1.jsonl", self.second_pass)
        self.assertIn("record_bytes_limit", report["reasons"])
        self.assertIn("launch_unresolved", report["reasons"])

    def test_changed_source_prefix_has_only_negative_authority(self):
        self.graph(("a5", "a1", "a7"), {"a1": ("a5", "a9"), "a7": ("a9",)})
        original, changed = fs.Opened.verify, []
        def change_at_verification(opened, expected=None):
            if (opened.name == "agent-a1.jsonl" and not changed
                    and opened.handle.tell() == opened.info.st_size):
                changed.append(True)
                # Mutate only our synthetic source after its admitted extent was read.
                with (self.subagents / "agent-a1.jsonl").open("ab") as handle:
                    handle.write(b"{}\n")
            return original(opened, expected)
        with mock.patch.object(fs.Opened, "verify", new=change_at_verification):
            report, opened, actors, key = self.collect()
        self.assertEqual(changed, [True])
        self.failed_scan_assertions(report, opened, actors, key, "source_changed")
        self.assertIn("agent-a7.jsonl", opened)

    def test_global_aggregate_refusal_stops_later_positive_sources(self):
        self.graph(("a5", "a1", "a7"), {"a1": ("a5", "a9")})
        limit = self.transcript.stat().st_size + (self.subagents / "agent-a5.jsonl").stat().st_size + 1
        with mock.patch.object(fs, "MAX_AGGREGATE_BYTES", limit):
            report, opened, actors, key = self.collect()
        self.assertIsNone(actors[key("a1")])
        self.assertNotIn("agent-a7.jsonl", opened)
        self.assertNotIn("agent-a9.jsonl", opened)
        self.assertNotIn("agent-a1.jsonl", self.second_pass)
        self.assertEqual(len(report["sources"]), 2)
        self.assertEqual(len(actors), 2)
        self.assertEqual(actors[key("a5")]["coverage"], "complete")
        self.assertIn("aggregate_bytes_limit", report["reasons"])

    def test_per_file_extent_refusal_does_not_revoke_unrelated_authority(self):
        self.graph(("a1", "a7"), {"a1": ("a9",), "a7": ("a9",)})
        self.append_large_record("a1")
        with mock.patch.object(fs, "MAX_TRANSCRIPT_BYTES", 10000):
            report, opened, actors, key = self.collect()
        self.assertIsNone(actors[key("a1")])
        self.assertNotIn("agent-a1.jsonl", self.first_pass)
        self.assertNotIn("agent-a1.jsonl", self.second_pass)
        self.assertIn("agent-a9.jsonl", opened)
        self.assertEqual(actors[key("a9")]["coverage"], "complete")
        self.assertIn("transcript_bytes_limit", report["reasons"])

    def test_tool_limit_refuses_entry_before_retention_and_preserves_partial_projection(self):
        import ao_evidence_audit
        self.graph(("a1",), {"a1": ("a5", "a9")})
        limits = ao_evidence_audit._limits()
        limits["tool_ids"] = 1
        collector = native.Collector(limits, set())
        scan = native.RecordScanner(collector, "a" * 64, "child", "a1", NATIVE_UUID,
                                    str(self.workspace), str(self.evidence))
        records = self.claims("a1", ("a5", "a9"))
        for number, record in enumerate(records[:2], 1):
            scan.record(json.dumps(record).encode(), True, number)
        with self.assertRaises(fs.SourceError) as caught:
            scan.record(json.dumps(records[2]).encode(), True, 3)
        self.assertEqual(caught.exception.reason, "tool_id_limit")
        self.assertEqual(collector.tool_ids, 2)
        self.assertNotIn("claim-1", scan.tool_entries)
        scope = native.Interval("terminal", 0, native.parse_timestamp("2026-01-01T00:00:10+00:00"),
                                None, native.parse_timestamp("2026-01-01T00:00:40+00:00"))
        candidates = native.launch_candidates(scan, scope)
        self.assertEqual([item.agent_id for item in candidates], ["a5"])


if __name__ == "__main__":
    unittest.main()
