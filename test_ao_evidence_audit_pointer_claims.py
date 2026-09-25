"""R58-CR-1: a contradictory parent pointer revokes authority, not negative evidence.

All ownership records, transcripts and files are synthetic. Real scanner/candidate and
collector code runs; descriptor observations distinguish forbidden new paths from
required rechecks of sources already read.
"""
import json
import unittest
from unittest import mock

import ao_evidence_audit_io as fs
import test_ao_evidence_audit_acceptance as acceptance
from test_ao_evidence_audit import AuditFixture, canon, completed_result


class PointerClaimTests(AuditFixture, unittest.TestCase):
    stamp = staticmethod(acceptance.CollectorAcceptanceTests.stamp)
    record = acceptance.CollectorAcceptanceTests.record
    launch = acceptance.CollectorAcceptanceTests.launch
    read = acceptance.CollectorAcceptanceTests.read

    def fixture(self, pointer_on="assistant", target="a9", roots=("a1", "a7", "a9"),
                *, novel=True, later_claim=None, descendant=False, mutation=None):
        self.build()
        parent = [self.human()]
        for actor in roots:
            parent.extend(self.launch(None, actor))
        self.write_transcript(parent)
        records = self.launch("a1", target, 14, 30, uuid="nested-claim")
        for index in (0, 1):
            if pointer_on == "both" or pointer_on == ("assistant" if index == 0 else "user"):
                records[index]["sourceToolAssistantUUID"] = "launch-a7"
        if mutation is not None:
            mutation(records)
        if novel:
            records.extend(self.launch("a1", "new-id", 15, 29))
        self.write_child("a1", records)
        self.write_child("a7", [self.record("a7", "assistant", "prose", 20, "Done.")])
        records = self.read("a9", 20, 21)
        if later_claim:
            records.extend(self.launch("a9", later_claim, 15, 29))
        if descendant:
            records.extend(self.launch("a9", "descendant", 15, 29))
            self.write_child("descendant", self.read("descendant", 20, 21)
                             + self.launch("descendant", "never-open", 22, 25))
            self.write_child("never-open", self.read("never-open", 23, 24))
        self.write_child("a9", records)
        for actor in {target, later_claim, "new-id"} - {None, "a1", "a7", "a9"}:
            self.write_child(actor, self.read(actor, 20, 21))

    def collect(self):
        opened, first_pass, second_pass = [], [], []
        original_open, original_scan, original_rehash = fs._open_component, fs.scan_source, fs.rehash_source
        def observe(name, *args):
            if name.startswith("agent-"):
                opened.append(name)
            return original_open(name, *args)
        def scan(opened_file, *args):
            first_pass.append(opened_file.name)
            return original_scan(opened_file, *args)
        def rehash(opened_file, *args):
            second_pass.append(opened_file.name)
            return original_rehash(opened_file, *args)
        with mock.patch.object(fs, "_open_component", side_effect=observe), \
             mock.patch.object(fs, "scan_source", side_effect=scan), \
             mock.patch.object(fs, "rehash_source", side_effect=rehash):
            report = self.audit_in_process()
        actors = {entry["actor_sha256"]: entry["observation"] for entry in report["children"]}
        def key(actor):
            return canon({"owner_sha256": report["owner_sha256"], "kind": "child", "agent_id": actor})
        return report, opened, first_pass, second_pass, actors, key

    def test_cli_pointer_contradiction_cannot_hide_sibling_reuse(self):
        self.fixture()
        report = self.report()
        actors = {entry["actor_sha256"]: entry["observation"] for entry in report["children"]}
        a9 = canon({"owner_sha256": report["owner_sha256"], "kind": "child", "agent_id": "a9"})
        self.assertIsNone(actors[a9])
        self.assertEqual(report["coverage"], "incomplete")
        self.assertIn("child_attribution_ambiguous", report["reasons"])

    def test_assistant_and_result_pointer_claims_are_negative_only(self):
        for pointer_on in ("assistant", "user", "both"):
            with self.subTest(pointer_on=pointer_on):
                self.reset()
                self.fixture(pointer_on)
                report, opened, first, second, actors, key = self.collect()
                self.assertIsNone(actors[key("a1")])
                self.assertIsNone(actors[key("a9")])
                self.assertEqual(actors[key("a7")]["coverage"], "complete")
                self.assertNotIn("agent-new-id.jsonl", opened)
                self.assertNotIn(key("new-id"), actors)
                self.assertIn("agent-a9.jsonl", first)
                self.assertIn("agent-a9.jsonl", second)
                self.assertEqual(len(report["sources"]), 4)

    def test_rejected_new_id_blocks_later_claim_without_ever_opening_it(self):
        for pointer_on in ("assistant", "user"):
            with self.subTest(pointer_on=pointer_on):
                self.reset()
                self.fixture(pointer_on, target="new-id", novel=False, later_claim="new-id")
                report, opened, first, second, actors, key = self.collect()
                self.assertIsNone(actors[key("a1")])
                self.assertNotIn("agent-new-id.jsonl", opened)
                self.assertNotIn(key("new-id"), actors)
                self.assertEqual(len(report["sources"]), 4)

    def test_later_negative_claim_nulls_already_read_descendants_and_stops_expansion(self):
        self.fixture("user", roots=("a9", "a7", "a1"), descendant=True)
        report, opened, first, second, actors, key = self.collect()
        for actor in ("a1", "a9", "descendant"):
            self.assertIsNone(actors[key(actor)])
        self.assertIn("agent-descendant.jsonl", first)
        self.assertIn("agent-descendant.jsonl", second)
        self.assertNotIn("agent-never-open.jsonl", opened)
        self.assertNotIn("agent-new-id.jsonl", opened)

    def test_incomplete_error_or_conflicting_result_never_invents_a_claim(self):
        def missing_result(records):
            del records[1:]
        def error_result(records):
            records[1]["message"]["content"][0]["is_error"] = True
        def conflicting_results(records):
            conflict = json.loads(json.dumps(records[1]))
            conflict["uuid"] = "different-result"
            conflict["toolUseResult"] = completed_result("different-id", "Inspect a9")
            records.append(conflict)
        def malformed_input(records):
            records[0]["message"]["content"][0]["input"] = None
        for mutation in (missing_result, error_result, conflicting_results, malformed_input):
            with self.subTest(mutation=mutation.__name__):
                self.reset()
                self.fixture("assistant", mutation=mutation)
                report, opened, first, second, actors, key = self.collect()
                self.assertIsNone(actors[key("a1")])
                self.assertEqual(actors[key("a9")]["coverage"], "complete")
                self.assertNotIn("agent-different-id.jsonl", opened)
                self.assertNotIn("agent-new-id.jsonl", opened)

    def test_invalid_actor_record_or_timestamp_does_not_supply_a_negative_id(self):
        for field, value in (("sessionId", "wrong"), ("cwd", "/unrelated"), ("agentId", "other"),
                             ("isSidechain", False), ("uuid", None), ("timestamp", "invalid")):
            with self.subTest(field=field):
                self.reset()
                def mutate(records):
                    records[0][field] = value
                self.fixture("assistant", mutation=mutate, novel=False)
                report, opened, first, second, actors, key = self.collect()
                self.assertEqual(actors[key("a9")]["coverage"], "complete")
                self.assertNotEqual(report["coverage"], "complete")

    def test_parsed_pointer_claims_keep_the_global_id_limit_and_failed_scan_latch(self):
        for pointer_on in ("assistant", "user"):
            with self.subTest(pointer_on=pointer_on):
                self.reset()
                self.fixture(pointer_on, roots=("a9", "a1", "a7"), novel=False)
                path = self.subagents / "agent-a1.jsonl"
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(self.record("a1", "user", "overflow", 31,
                        [{"type": "tool_result", "tool_use_id": "over-limit"}])) + "\n")
                # Three parent launch IDs, a9's Read ID, a1's completed launch ID.
                with mock.patch.object(fs, "MAX_TOOL_IDS", 5):
                    report, opened, first, second, actors, key = self.collect()
                self.assertIn("tool_id_limit", report["reasons"])
                self.assertIsNone(actors[key("a1")])
                self.assertIsNone(actors[key("a9")])
                self.assertNotIn("agent-a7.jsonl", opened)
                self.assertNotIn("agent-a1.jsonl", second)
                self.assertEqual(opened.count("agent-a1.jsonl"), 1)
                self.assertNotIn(key("a1"), {entry["actor_sha256"] for entry in report["sources"]})

    def test_pointer_claim_prefix_survives_later_record_byte_failure(self):
        self.fixture("both", novel=False)
        with (self.subagents / "agent-a1.jsonl").open("ab") as handle:
            handle.write(b'"' + b"x" * 20000 + b'"\n')
        with mock.patch.object(fs, "MAX_RECORD_BYTES", 2000):
            report, opened, first, second, actors, key = self.collect()
        self.assertIsNone(actors[key("a1")])
        self.assertIsNone(actors[key("a9")])
        self.assertIn("record_bytes_limit", report["reasons"])
        self.assertNotIn("agent-a1.jsonl", second)
        self.assertEqual(opened.count("agent-a1.jsonl"), 1)
        self.assertNotIn(key("a1"), {entry["actor_sha256"] for entry in report["sources"]})


if __name__ == "__main__":
    unittest.main()
