"""Synthetic offline tests for explicitly attributed native and delegate usage."""

import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch

import ao_evidence_audit_io as audit_io
import ao_usage_audit
import deepseek_adapter
from test_ao_evidence_audit import AuditFixture, completed_result


ROOT = Path(__file__).resolve().parent
NO_DELEGATE = {"provider": "none", "inventory": {"files": {}}, "files": {}, "policy_path": "p"}
USAGE_KEYS = {"input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"}
TOP_KEYS = {"version", "kind", "scope", "coverage", "request_sha256", "owner_sha256", "work_unit",
            "sources", "parent", "children", "child_coverage", "native_totals", "parent_rollup",
            "ao_primary_counter_relation", "api_delegates", "reasons", "limitations"}


class NativeUsageAuditTests(AuditFixture, unittest.TestCase):
    def assistant(self, uuid, timestamp, message_id, usage, model="claude-fable-5-1"):
        record = self.parent_record("assistant", uuid, timestamp, [])
        record["message"].update({"id": message_id, "model": model, "usage": usage})
        return record

    def child_assistant(self, uuid, timestamp, message_id, usage, model="claude-sonnet-4-5", agent="a1"):
        record = self.child_record("assistant", uuid, timestamp, [], agent=agent)
        record["message"].update({"id": message_id, "model": model, "usage": usage})
        return record

    @staticmethod
    def counts(input_tokens, output_tokens, creation, read):
        return {"input_tokens": input_tokens, "output_tokens": output_tokens,
                "cache_creation_input_tokens": creation, "cache_read_input_tokens": read}

    def report(self, records, **kwargs):
        self.write_transcript(records)
        kwargs.setdefault("delegate", NO_DELEGATE)
        self.build_without_delegate(**kwargs)
        return ao_usage_audit.audit(self.home, self.room, self.request_id, self.database)

    def run_cli(self):
        command = [sys.executable, str(ROOT / "project_room.py"), "ao-native-usage-audit",
                   "--home", str(self.home), "--room", self.room, "--request", self.request_id,
                   "--ao-database", str(self.database)]
        return subprocess.run(command, capture_output=True, text=True, timeout=300, env=self.environment())

    def build_without_delegate(self, **kwargs):
        kwargs.setdefault("delegate", NO_DELEGATE)
        preparation_overrides = dict(kwargs.pop("preparation_overrides", {}))
        preparation_overrides.setdefault("provider", "none")
        self.build(preparation_overrides=preparation_overrides, **kwargs)

    def launch_records(self, *, background=False, result_usage=None):
        prompt = "Implement the isolated child task."
        launch = self.assistant("launch-record", "2026-01-01T00:00:11+00:00", "launch-message",
                                {"input_tokens": 0, "output_tokens": 0,
                                 "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0})
        launch["message"]["content"] = [{"type": "tool_use", "id": "agent-tool", "name": "Agent",
                                         "input": {"prompt": prompt, "subagent_type": "pr-sonnet",
                                                   **({"run_in_background": True} if background else {})}}]
        result = completed_result("a1", prompt)
        if result_usage is not None:
            result["usage"] = result_usage
            result["totalTokens"] = 999999
        completed = self.user_results(
            "result-record", "2026-01-01T00:00:30+00:00",
            [{"type": "tool_result", "tool_use_id": "agent-tool", "content": "done"}],
            toolUseResult=result, sourceToolAssistantUUID="launch-record")
        return launch, completed

    def build_child_routing(self):
        self.prepared["routing"]["agents"] = {"pr-sonnet": "configured-sonnet"}
        room = self.home / "ao" / "rooms" / self.room
        (room / self.state["preparation"]).write_text(json.dumps(self.prepared, indent=2) + "\n", encoding="utf-8")
        self.state["preparation_sha256"] = hashlib.sha256(
            json.dumps(self.prepared, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                       allow_nan=False).encode("utf-8")).hexdigest()
        self.write_state()

    def test_parent_only_totals_and_proved_zero_workers(self):
        records = [self.human(),
                   self.assistant("a-1", "2026-01-01T00:00:12+00:00", "message-1",
                                  {"input_tokens": 10, "output_tokens": 20,
                                   "cache_creation_input_tokens": 3, "cache_read_input_tokens": 4}),
                   self.assistant("a-2", "2026-01-01T00:00:13+00:00", "message-2",
                                  {"input_tokens": 2, "output_tokens": 5,
                                   "cache_creation_input_tokens": 1, "cache_read_input_tokens": 6})]
        report = self.report(records)
        self.assertEqual(report["coverage"], "complete")
        self.assertEqual(report["parent"]["counters"], self.counts(12, 25, 4, 10))
        self.assertEqual(report["parent"]["responses"], 2)
        self.assertEqual(report["parent"]["reasons"], [])
        self.assertEqual(report["children"], [])
        self.assertEqual(report["child_coverage"], "complete")
        self.assertEqual(report["native_totals"]["primary"], self.counts(12, 25, 4, 10))
        self.assertEqual(report["native_totals"]["workers"], self.counts(0, 0, 0, 0))
        self.assertEqual(report["native_totals"]["workers_by_attested_model"], {})
        self.assertEqual(report["native_totals"]["combined"], self.counts(12, 25, 4, 10))
        self.assertEqual(report["parent_rollup"],
                         {"basis": "actor_partitioned_records", "parent_counts_include_children": False,
                          "reported_child_summaries_used": False})

    def test_repeated_streaming_snapshots_count_once_and_use_max_output(self):
        usage = {"input_tokens": 4, "output_tokens": 7,
                 "cache_creation_input_tokens": 2, "cache_read_input_tokens": 1}
        records = [self.human(), self.assistant("a-1", "2026-01-01T00:00:12+00:00", "stream-id", usage),
                   self.assistant("a-2", "2026-01-01T00:00:13+00:00", "stream-id", usage),
                   self.assistant("a-3", "2026-01-01T00:00:14+00:00", "stream-id", usage)]
        report = self.report(records)
        self.assertEqual(report["parent"]["coverage"], "complete")
        self.assertEqual(report["parent"]["counters"], self.counts(4, 7, 2, 1))
        self.assertEqual(report["parent"]["responses"], 1)
        self.assertEqual(report["parent"]["repeated_response_records"], 2)
        self.assertEqual(report["parent"]["reasons"], [])

        records[-1]["message"]["usage"]["output_tokens"] = 40
        report = self.report(records)
        self.assertEqual(report["parent"]["counters"], self.counts(4, 40, 2, 1))
        self.assertEqual(report["parent"]["repeated_response_records"], 2)
        self.assertEqual(report["parent"]["coverage"], "complete")
        self.assertEqual(report["parent"]["reasons"], [])

    def test_same_parent_message_id_with_missing_usage_is_excluded(self):
        valid = self.assistant("valid-record", "2026-01-01T00:00:12+00:00", "shared-id",
                               {"input_tokens": 4, "output_tokens": 7,
                                "cache_creation_input_tokens": 2, "cache_read_input_tokens": 1})
        missing = self.assistant("missing-record", "2026-01-01T00:00:13+00:00", "shared-id", None)
        report = self.report([self.human(), valid, missing])
        self.assertEqual(report["parent"]["coverage"], "incomplete")
        self.assertEqual(report["parent"]["responses"], 0)
        self.assertEqual(report["parent"]["reasons"], ["usage_unattributable"])
        self.assertEqual(report["parent"]["cache_coverage"], "incomplete")
        self.assertIsNone(report["native_totals"]["primary"])
        self.assertEqual(report["ao_primary_counter_relation"], "unavailable")

    def test_conflicting_message_id_invalidates_actor_totals(self):
        records = [self.human(),
                   self.assistant("a-1", "2026-01-01T00:00:12+00:00", "conflict-id",
                                  {"input_tokens": 4, "output_tokens": 7,
                                   "cache_creation_input_tokens": 2, "cache_read_input_tokens": 1}),
                   self.assistant("a-2", "2026-01-01T00:00:13+00:00", "conflict-id",
                                  {"input_tokens": 5, "output_tokens": 7,
                                   "cache_creation_input_tokens": 2, "cache_read_input_tokens": 1})]
        report = self.report(records)
        self.assertEqual(report["parent"]["coverage"], "incomplete")
        self.assertIn("usage_conflict", report["parent"]["reasons"])
        self.assertEqual(report["parent"]["reasons"], ["usage_conflict"])
        self.assertIsNone(report["native_totals"]["primary"])
        self.assertIsNone(report["native_totals"]["combined"])
        self.assertEqual(report["native_totals"]["coverage"], "incomplete")

    def test_exact_duplicate_uuid_record_is_not_counted_twice(self):
        record = self.assistant("same-record", "2026-01-01T00:00:12+00:00", "one-message",
                                {"input_tokens": 3, "output_tokens": 8,
                                 "cache_creation_input_tokens": 1, "cache_read_input_tokens": 2})
        report = self.report([self.human(), record, record.copy()])
        self.assertEqual(report["parent"]["counters"], self.counts(3, 8, 1, 2))
        self.assertEqual(report["parent"]["responses"], 1)
        self.assertEqual(report["parent"]["repeated_response_records"], 0)
        self.assertEqual(report["parent"]["reasons"], [])

    def test_child_usage_is_partitioned_and_configured_identity_is_not_attested(self):
        launch, completed = self.launch_records(result_usage={"input_tokens": 99999, "output_tokens": 99999,
                                                               "cache_creation_input_tokens": 99999,
                                                               "cache_read_input_tokens": 99999,
                                                               "server_tool_use": None, "service_tier": None,
                                                               "cache_creation": None})
        child_usage = {"input_tokens": 5, "output_tokens": 6,
                       "cache_creation_input_tokens": 1, "cache_read_input_tokens": 2}
        self.write_transcript([self.human(), launch, completed])
        self.write_child("a1", [self.child_assistant("child-response", "2026-01-01T00:00:20+00:00",
                                                    "child-message", child_usage)])
        self.build_without_delegate()
        self.build_child_routing()
        report = ao_usage_audit.audit(self.home, self.room, self.request_id, self.database)
        self.assertEqual(report["coverage"], "complete")
        child = report["children"][0]["usage"]
        self.assertEqual(report["children"][0]["configured_agent"], "pr-sonnet")
        self.assertEqual(child["configured_model"], "configured-sonnet")
        self.assertEqual(child["attested_models"], ["claude-sonnet-4-5"])
        self.assertEqual(child["counters"], self.counts(5, 6, 1, 2))
        self.assertEqual(report["native_totals"]["primary"], self.counts(0, 0, 0, 0))
        self.assertEqual(report["native_totals"]["workers"], self.counts(5, 6, 1, 2))
        self.assertEqual(report["native_totals"]["combined"], self.counts(5, 6, 1, 2))
        self.assertEqual(report["reasons"], [])

    def test_nested_synchronous_grandchild_is_a_separate_worker(self):
        launch, completed = self.launch_records()
        prompt = "Implement the nested isolated task."
        child_launch = self.child_assistant(
            "child-launch-record", "2026-01-01T00:00:20+00:00", "child-launch-message",
            {"input_tokens": 5, "output_tokens": 6,
             "cache_creation_input_tokens": 1, "cache_read_input_tokens": 2})
        child_launch["message"]["content"] = [{"type": "tool_use", "id": "grandchild-tool", "name": "Agent",
                                               "input": {"prompt": prompt, "subagent_type": "pr-sonnet"}}]
        child_completed = self.child_record(
            "user", "grandchild-result-record", "2026-01-01T00:00:25+00:00",
            [{"type": "tool_result", "tool_use_id": "grandchild-tool", "content": "done"}],
            toolUseResult=completed_result("g1", prompt),
            sourceToolAssistantUUID="child-launch-record")
        grandchild_response = self.child_assistant(
            "grandchild-response", "2026-01-01T00:00:22+00:00", "grandchild-message",
            {"input_tokens": 2, "output_tokens": 3,
             "cache_creation_input_tokens": 1, "cache_read_input_tokens": 1},
            agent="g1")
        self.write_transcript([self.human(), launch, completed])
        self.write_child("a1", [child_launch, child_completed])
        self.write_child("g1", [grandchild_response])
        self.build_without_delegate()
        self.build_child_routing()
        report = ao_usage_audit.audit(self.home, self.room, self.request_id, self.database)
        self.assertEqual(len(report["children"]), 2)
        self.assertEqual(
            {tuple(sorted(child["usage"]["counters"].items())) for child in report["children"]},
            {tuple(sorted(self.counts(5, 6, 1, 2).items())),
             tuple(sorted(self.counts(2, 3, 1, 1).items()))})
        self.assertEqual(report["native_totals"]["workers"], self.counts(7, 9, 2, 3))
        self.assertEqual(report["native_totals"]["workers_by_attested_model"],
                         {"claude-sonnet-4-5": self.counts(7, 9, 2, 3)})
        self.assertEqual(report["coverage"], "complete")

    def test_child_responses_using_two_models_are_attributed_per_response(self):
        launch, completed = self.launch_records()
        first = self.child_assistant(
            "child-response-1", "2026-01-01T00:00:20+00:00", "child-message-1",
            {"input_tokens": 2, "output_tokens": 3,
             "cache_creation_input_tokens": 1, "cache_read_input_tokens": 1},
            model="claude-sonnet-4-5")
        second = self.child_assistant(
            "child-response-2", "2026-01-01T00:00:21+00:00", "child-message-2",
            {"input_tokens": 4, "output_tokens": 5,
             "cache_creation_input_tokens": 2, "cache_read_input_tokens": 3},
            model="claude-opus-4-1")
        self.write_transcript([self.human(), launch, completed])
        self.write_child("a1", [first, second])
        self.build_without_delegate()
        self.build_child_routing()
        report = ao_usage_audit.audit(self.home, self.room, self.request_id, self.database)
        self.assertEqual(report["children"][0]["usage"]["attested_models"],
                         ["claude-opus-4-1", "claude-sonnet-4-5"])
        self.assertEqual(report["native_totals"]["workers"], self.counts(6, 8, 3, 4))
        self.assertEqual(report["native_totals"]["workers_by_attested_model"],
                         {"claude-opus-4-1": self.counts(4, 5, 2, 3),
                          "claude-sonnet-4-5": self.counts(2, 3, 1, 1)})
        self.assertNotIn("counters_by_model", report["children"][0])
        self.assertNotIn("counters_by_model", report["children"][0]["usage"])
        self.assertEqual(report["coverage"], "complete")
        self.assertNotIn("worker_model_mixed", report["reasons"])

    def test_cross_actor_message_id_conflict_invalidates_both_actors(self):
        launch, completed = self.launch_records()
        parent_response = self.assistant("parent-response", "2026-01-01T00:00:20+00:00", "reused-id",
                                         {"input_tokens": 3, "output_tokens": 4,
                                          "cache_creation_input_tokens": 1, "cache_read_input_tokens": 1})
        self.write_transcript([self.human(), launch, parent_response, completed])
        self.write_child("a1", [self.child_assistant("child-response", "2026-01-01T00:00:21+00:00",
                                                    "reused-id", {"input_tokens": 5, "output_tokens": 6,
                                                                  "cache_creation_input_tokens": 2,
                                                                  "cache_read_input_tokens": 2})])
        self.build_without_delegate()
        self.build_child_routing()
        report = ao_usage_audit.audit(self.home, self.room, self.request_id, self.database)
        self.assertIn("usage_conflict", report["parent"]["reasons"])
        self.assertIn("usage_conflict", report["children"][0]["usage"]["reasons"])
        self.assertEqual(report["parent"]["reasons"], ["usage_conflict"])
        self.assertEqual(report["children"][0]["usage"]["reasons"], ["usage_conflict"])
        self.assertIsNone(report["native_totals"]["primary"])
        self.assertIsNone(report["native_totals"]["workers"])

    def test_unknown_child_configuration_does_not_claim_or_invalidate_identity(self):
        launch, completed = self.launch_records()
        self.write_transcript([self.human(), launch, completed])
        self.write_child("a1", [self.child_assistant("child-response", "2026-01-01T00:00:20+00:00",
                                                    "child-message", {"input_tokens": 2, "output_tokens": 3,
                                                                      "cache_creation_input_tokens": 1,
                                                                      "cache_read_input_tokens": 1})])
        self.build_without_delegate()
        report = ao_usage_audit.audit(self.home, self.room, self.request_id, self.database)
        child = report["children"][0]["usage"]
        self.assertIsNone(child["configured_model"])
        self.assertEqual(child["attested_models"], ["claude-sonnet-4-5"])
        self.assertIn("configured_identity_unknown", child["reasons"])
        self.assertEqual(child["reasons"], ["configured_identity_unknown"])
        self.assertEqual(child["coverage"], "complete")
        self.assertEqual(report["coverage"], "complete")
        self.assertEqual(report["reasons"], ["configured_identity_unknown"])

    def test_missing_and_background_children_keep_workers_null(self):
        launch, completed = self.launch_records()
        missing = self.report([self.human(), launch, completed])
        self.assertEqual(missing["child_coverage"], "incomplete")
        self.assertIsNone(missing["native_totals"]["workers"])
        self.assertIsNone(missing["native_totals"]["workers_by_attested_model"])
        self.assertIsNone(missing["native_totals"]["combined"])
        self.assertIsNotNone(missing["native_totals"]["primary"])
        self.assertIn("child_missing", missing["reasons"])
        self.assertEqual(missing["native_totals"]["primary"], self.counts(0, 0, 0, 0))
        self.assertEqual(missing["reasons"], ["child_missing", "source_missing"])

        launch, completed = self.launch_records(background=True)
        self.write_transcript([self.human(), launch, completed])
        self.write_child("a1", [self.child_assistant("child-response", "2026-01-01T00:00:20+00:00",
                                                    "child-message", {"input_tokens": 1, "output_tokens": 2,
                                                                      "cache_creation_input_tokens": 0,
                                                                      "cache_read_input_tokens": 0})])
        self.build_without_delegate()
        background = ao_usage_audit.audit(self.home, self.room, self.request_id, self.database)
        self.assertEqual(background["child_coverage"], "incomplete")
        self.assertIsNone(background["children"][0]["usage"])
        self.assertIsNone(background["native_totals"]["workers"])
        self.assertIsNone(background["native_totals"]["workers_by_attested_model"])
        self.assertIn("child_interval_unbound", background["reasons"])
        self.assertEqual(background["native_totals"]["primary"], self.counts(0, 0, 0, 0))
        self.assertEqual(background["reasons"], ["child_interval_unbound"])

    def test_unknown_transcript_subagent_type_is_never_echoed(self):
        launch, completed = self.launch_records()
        private = "PRIVATE-arbitrary-text"
        launch["message"]["content"][0]["input"]["subagent_type"] = private
        self.write_transcript([self.human(), launch, completed])
        self.write_child("a1", [self.child_assistant(
            "child-response", "2026-01-01T00:00:20+00:00", "child-message",
            {"input_tokens": 2, "output_tokens": 3,
             "cache_creation_input_tokens": 1, "cache_read_input_tokens": 1})])
        self.build_without_delegate()
        self.build_child_routing()
        report = ao_usage_audit.audit(self.home, self.room, self.request_id, self.database)
        self.assertIsNone(report["children"][0]["configured_agent"])
        self.assertIn("configured_identity_unknown", report["children"][0]["usage"]["reasons"])
        process = self.run_cli()
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertNotIn(private, process.stdout)

    def test_records_outside_selected_parent_interval_are_excluded(self):
        before = self.assistant("before", "2026-01-01T00:00:05+00:00", "before-id",
                                {"input_tokens": 99, "output_tokens": 99,
                                 "cache_creation_input_tokens": 99, "cache_read_input_tokens": 99})
        selected = self.assistant("inside", "2026-01-01T00:00:20+00:00", "inside-id",
                                  {"input_tokens": 4, "output_tokens": 8,
                                   "cache_creation_input_tokens": 1, "cache_read_input_tokens": 2})
        after = self.assistant("after", "2026-01-01T00:00:30+00:00", "after-id",
                               {"input_tokens": 77, "output_tokens": 88,
                                "cache_creation_input_tokens": 77, "cache_read_input_tokens": 88})
        next_human = self.human("next-human", "2026-01-01T00:00:25+00:00", "Next request.")
        report = self.report([before, self.human(), selected, next_human, after])
        self.assertEqual(report["parent"]["coverage"], "complete")
        self.assertEqual(report["parent"]["counters"], self.counts(4, 8, 1, 2))
        self.assertEqual(report["parent"]["responses"], 1)
        self.assertEqual(report["reasons"], [])

    def test_compaction_marker_makes_in_interval_usage_incomplete(self):
        compact = self.parent_record("system", "compact", "2026-01-01T00:00:15+00:00", "")
        compact["subtype"] = "compact_boundary"
        response = self.assistant("assistant", "2026-01-01T00:00:20+00:00", "message",
                                  {"input_tokens": 1, "output_tokens": 2,
                                   "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0})
        report = self.report([self.human(), compact, response])
        self.assertEqual(report["parent"]["coverage"], "incomplete")
        self.assertIn("compaction_in_interval", report["parent"]["reasons"])
        self.assertEqual(report["parent"]["reasons"], ["compaction_in_interval"])
        self.assertIsNone(report["native_totals"]["primary"])

    def test_compaction_marker_without_timestamp_in_interval_is_incomplete(self):
        compact = self.parent_record("system", "compact", "2026-01-01T00:00:15+00:00", "")
        compact.pop("timestamp")
        compact["subtype"] = "compact_boundary"
        response = self.assistant("assistant", "2026-01-01T00:00:20+00:00", "message",
                                  {"input_tokens": 1, "output_tokens": 2,
                                   "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0})
        report = self.report([self.human(), compact, response])
        self.assertEqual(report["parent"]["coverage"], "incomplete")
        self.assertIn("compaction_in_interval", report["parent"]["reasons"])

    def test_child_compaction_marker_without_timestamp_is_incomplete_not_an_error(self):
        launch, completed = self.launch_records()
        compact = self.child_record("system", "child-compact", "2026-01-01T00:00:15+00:00", "")
        compact.pop("timestamp")
        compact["subtype"] = "compact_boundary"
        self.write_transcript([self.human(), launch, completed])
        self.write_child("a1", [compact, self.child_assistant(
            "child-response", "2026-01-01T00:00:20+00:00", "child-message",
            {"input_tokens": 5, "output_tokens": 6, "cache_creation_input_tokens": 1, "cache_read_input_tokens": 2})])
        self.build_without_delegate()
        self.build_child_routing()
        report = ao_usage_audit.audit(self.home, self.room, self.request_id, self.database)
        self.assertEqual(report["parent"]["coverage"], "complete")
        child = report["children"][0]["usage"]
        self.assertEqual(child["coverage"], "incomplete")
        self.assertIn("compaction_in_interval", child["reasons"])
        self.assertEqual(report["child_coverage"], "incomplete")
        self.assertIsNone(report["native_totals"]["workers"])

    def test_unknown_cache_splits_do_not_invalidate_token_coverage(self):
        response = self.assistant("assistant", "2026-01-01T00:00:20+00:00", "message",
                                  {"input_tokens": 12, "output_tokens": 4})
        report = self.report([self.human(), response])
        self.assertEqual(report["coverage"], "complete")
        self.assertEqual(report["parent"]["coverage"], "complete")
        self.assertEqual(report["parent"]["cache_coverage"], "incomplete")
        self.assertEqual(report["parent"]["counters"],
                         {"input_tokens": 12, "output_tokens": 4,
                          "cache_creation_input_tokens": None, "cache_read_input_tokens": None})
        self.assertIn("cache_split_unavailable", report["parent"]["reasons"])
        self.assertEqual(report["parent"]["reasons"], ["cache_split_unavailable"])

    def test_synthetic_model_response_has_no_token_counters(self):
        response = self.assistant("assistant", "2026-01-01T00:00:20+00:00", "synthetic-id", {},
                                  model="<synthetic>")
        response["message"].pop("usage")
        report = self.report([self.human(), response])
        self.assertEqual(report["parent"]["coverage"], "complete")
        self.assertEqual(report["parent"]["synthetic_responses"], 1)
        self.assertEqual(report["parent"]["responses"], 0)
        self.assertEqual(report["parent"]["counters"], self.counts(0, 0, 0, 0))
        self.assertEqual(report["parent"]["reasons"], [])

    def test_invalid_attested_model_strings_are_unattributable_and_private(self):
        for index, model in enumerate(("bad model\nname", "x" * 200)):
            with self.subTest(index=index):
                self.reset()
                response = self.assistant(
                    "invalid-model-response", "2026-01-01T00:00:20+00:00", "invalid-model-message",
                    {"input_tokens": 1, "output_tokens": 2,
                     "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
                    model=model)
                report = self.report([self.human(), response])
                self.assertEqual(report["parent"]["coverage"], "incomplete")
                self.assertEqual(report["parent"]["responses"], 0)
                self.assertEqual(report["parent"]["reasons"], ["usage_unattributable"])
                self.assertIsNone(report["native_totals"]["primary"])
                process = self.run_cli()
                self.assertEqual(process.returncode, 1, process.stderr)
                self.assertNotIn(model, process.stdout)
                self.assertNotIn(json.dumps(model)[1:-1], process.stdout)

    def test_invalid_timestamp_in_parent_transcript_does_not_crash(self):
        response = self.assistant(
            "untimed-response", "not-a-timestamp", "untimed-message",
            {"input_tokens": 1, "output_tokens": 2,
             "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0})
        report = self.report([self.human(), response])
        self.assertEqual(report["coverage"], "incomplete")
        self.assertEqual(report["parent"]["responses"], 0)
        self.assertIn("source_malformed", report["parent"]["reasons"])

    def test_source_mutation_between_passes_marks_source_changed(self):
        response = self.assistant("assistant", "2026-01-01T00:00:20+00:00", "message",
                                  {"input_tokens": 1, "output_tokens": 2,
                                   "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0})
        self.write_transcript([self.human(), response])
        self.build_without_delegate()
        def changed(opened, *args):
            opened.close()
            raise audit_io.SourceError("source_changed")
        with patch.object(audit_io, "rehash_source", changed):
            report = ao_usage_audit.audit(self.home, self.room, self.request_id, self.database)
        self.assertEqual(report["coverage"], "incomplete")
        self.assertIn("source_changed", report["reasons"])
        self.assertEqual(report["reasons"], ["source_changed"])
        self.assertIn("source_changed", report["parent"]["reasons"])
        self.assertIsNone(report["native_totals"]["primary"])

    def test_ao_primary_counter_relation_is_observational(self):
        response = self.assistant("assistant", "2026-01-01T00:00:20+00:00", "message",
                                  {"input_tokens": 10, "output_tokens": 20,
                                   "cache_creation_input_tokens": 3, "cache_read_input_tokens": 4})
        parent = self.report([self.human(), response],
                             request_overrides={"usage": {"known": True, "inputTokens": 10,
                                                          "outputTokens": 20, "cachedTokens": 7}})
        self.assertEqual(parent["ao_primary_counter_relation"], "parent_sum")
        self.assertEqual(parent["parent"]["counters"], self.counts(10, 20, 3, 4))
        self.assertEqual(parent["reasons"], [])

        launch, completed = self.launch_records()
        self.write_transcript([self.human(), response, launch, completed])
        self.write_child("a1", [self.child_assistant("child", "2026-01-01T00:00:20+00:00", "child-id",
                                                    {"input_tokens": 2, "output_tokens": 3,
                                                     "cache_creation_input_tokens": 1,
                                                     "cache_read_input_tokens": 1})])
        self.build_without_delegate(
            request_overrides={"usage": {"known": True, "inputTokens": 12,
                                         "outputTokens": 23, "cachedTokens": 9}})
        self.build_child_routing()
        combined = ao_usage_audit.audit(self.home, self.room, self.request_id, self.database)
        self.assertEqual(combined["ao_primary_counter_relation"], "parent_and_workers_sum")
        self.assertEqual(combined["native_totals"]["primary"], self.counts(10, 20, 3, 4))
        self.assertEqual(combined["native_totals"]["workers"], self.counts(2, 3, 1, 1))
        self.assertEqual(combined["native_totals"]["combined"], self.counts(12, 23, 4, 5))
        self.assertEqual(combined["reasons"], [])

        unknown = self.report([self.human(), response])
        self.assertEqual(unknown["ao_primary_counter_relation"], "unavailable")

        neither = self.report(
            [self.human(), response],
            request_overrides={"usage": {"known": True, "inputTokens": 99,
                                         "outputTokens": 98, "cachedTokens": 97}})
        self.assertEqual(neither["ao_primary_counter_relation"], "neither")

    def create_ledger_job(self, db, *, job_id, created_at, usage, usage_source="final_chunk"):
        values = {"id": job_id, "room_id": self.room, "request_id": job_id,
                  "lane": "deep", "payload_sha256": "a" * 64, "profile_sha256": "b" * 64,
                  "state": "completed", "created_at": created_at, "requested_model": "deepseek-test",
                  "observed_model": "deepseek-observed", "thinking": "enabled", "max_tokens": 100,
                  "input_bytes": 20, "usage_json": json.dumps(usage), "usage_source": usage_source,
                  "reserved_bytes": 0}
        columns = tuple(values)
        db.execute("INSERT INTO jobs (%s) VALUES (%s)" %
                   (", ".join(columns), ", ".join("?" for _ in columns)), tuple(values.values()))

    def test_deepseek_window_allowlist_read_only_and_missing_provider_cases(self):
        self.write_transcript([self.human()])
        self.build()
        ledger = self.home / "deepseek" / "ledger.sqlite3"
        ledger.parent.mkdir(parents=True)
        with sqlite3.connect(ledger) as db:
            db.executescript(deepseek_adapter.SCHEMA)
            self.create_ledger_job(db, job_id="inside-job", created_at="2026-01-01T00:00:20+00:00",
                                   usage={"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15,
                                          "unexpected": 999,
                                          "prompt_tokens_details": {"cached_tokens": 4, "secret": "hidden"}})
            self.create_ledger_job(db, job_id="outside-job", created_at="2026-01-01T00:01:11+00:00",
                                   usage={"prompt_tokens": 99}, usage_source="usage_only_chunk")
        before = hashlib.sha256(ledger.read_bytes()).hexdigest()
        mtime = ledger.stat().st_mtime_ns
        report = ao_usage_audit.audit(self.home, self.room, self.request_id, self.database)
        self.assertEqual(report["api_delegates"],
                         {"coverage": "complete",
                          "jobs": [{"job_sha256": hashlib.sha256(b"inside-job").hexdigest(),
                                    "requested_model": "deepseek-test", "observed_model": "deepseek-observed",
                                    "usage_source": "final_chunk",
                                    "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15,
                                              "prompt_tokens_details": {"cached_tokens": 4}}}],
                          "reasons": []})
        self.assertEqual(hashlib.sha256(ledger.read_bytes()).hexdigest(), before)
        self.assertEqual(ledger.stat().st_mtime_ns, mtime)
        self.assertFalse(Path(str(ledger) + "-journal").exists())

        ledger.unlink()
        missing_report = ao_usage_audit.audit(self.home, self.room, self.request_id, self.database)
        self.assertEqual(missing_report["api_delegates"],
                         {"coverage": "unavailable", "reason": "delegate_ledger_absent"})

        none = self.report([self.human()], delegate=NO_DELEGATE)
        self.assertEqual(none["api_delegates"], {"coverage": "not_applicable"})

    def test_deepseek_content_chunk_usage_source_is_counted(self):
        self.write_transcript([self.human()])
        self.build()
        ledger = self.home / "deepseek" / "ledger.sqlite3"
        ledger.parent.mkdir(parents=True)
        usage = {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}
        with sqlite3.connect(ledger) as db:
            db.executescript(deepseek_adapter.SCHEMA)
            self.create_ledger_job(db, job_id="content-chunk",
                                   created_at="2026-01-01T00:00:30+00:00", usage=usage,
                                   usage_source="content_chunk")
        report = ao_usage_audit.audit(self.home, self.room, self.request_id, self.database)
        api = report["api_delegates"]
        self.assertEqual(api["coverage"], "complete")
        self.assertEqual(api["reasons"], ["delegate_usage_non_final_chunk"])
        self.assertNotIn("delegate_usage_source_unavailable", api["reasons"])
        self.assertEqual(api["jobs"][0]["usage_source"], "content_chunk")
        self.assertEqual(api["jobs"][0]["usage"], usage)
        self.assertEqual(report["coverage"], "complete")
        self.assertIn("delegate_usage_non_final_chunk", report["reasons"])
        process = self.run_cli()
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout)["coverage"], "complete")

    def test_deepseek_failed_missing_usage_boundaries_and_unsafe_ledger(self):
        self.write_transcript([self.human()])
        self.build()
        ledger = self.home / "deepseek" / "ledger.sqlite3"
        ledger.parent.mkdir(parents=True)
        with sqlite3.connect(ledger) as db:
            db.executescript(deepseek_adapter.SCHEMA)
            self.create_ledger_job(db, job_id="start-boundary", created_at="2026-01-01T00:00:10+00:00",
                                   usage={"prompt_tokens": 1, "completion_tokens": 2})
            self.create_ledger_job(db, job_id="end-boundary", created_at="2026-01-01T00:01:10+00:00",
                                   usage={"prompt_tokens": 3, "completion_tokens": 4})
            self.create_ledger_job(db, job_id="failed-no-usage", created_at="2026-01-01T00:00:30+00:00",
                                   usage={"prompt_tokens": 5, "completion_tokens": 6})
            db.execute("UPDATE jobs SET state='failed', usage_json=NULL WHERE id='failed-no-usage'")
        report = ao_usage_audit.audit(self.home, self.room, self.request_id, self.database)
        api = report["api_delegates"]
        self.assertEqual(api["coverage"], "incomplete")
        self.assertIn("delegate_usage_unavailable", api["reasons"])
        self.assertEqual(report["coverage"], "incomplete")
        jobs = {item["job_sha256"]: item for item in api["jobs"]}
        self.assertEqual(set(jobs), {hashlib.sha256(job_id.encode()).hexdigest()
                                    for job_id in ("start-boundary", "end-boundary", "failed-no-usage")})
        self.assertIsNone(jobs[hashlib.sha256(b"failed-no-usage").hexdigest()]["usage"])

        target = ledger.with_name("ledger-target.sqlite3")
        ledger.rename(target)
        ledger.symlink_to(target.name)
        symlink_report = ao_usage_audit.audit(self.home, self.room, self.request_id, self.database)
        self.assertEqual(symlink_report["api_delegates"],
                         {"coverage": "unavailable", "reason": "delegate_ledger_unsafe"})
        ledger.unlink()
        ledger.mkdir()
        non_regular_report = ao_usage_audit.audit(self.home, self.room, self.request_id, self.database)
        self.assertEqual(non_regular_report["api_delegates"],
                         {"coverage": "unavailable", "reason": "delegate_ledger_unsafe"})

    def test_delegate_window_is_inclusive_at_start_and_receipt_end(self):
        self.write_transcript([self.human()])
        self.build()
        ledger = self.home / "deepseek" / "ledger.sqlite3"
        ledger.parent.mkdir(parents=True)
        job_times = {
            "before-start": "2026-01-01T00:00:09+00:00",
            "at-start": "2026-01-01T00:00:10+00:00",
            "at-receipt-end": "2026-01-01T00:01:10+00:00",
            "after-receipt-end": "2026-01-01T00:01:11+00:00",
        }
        with sqlite3.connect(ledger) as db:
            db.executescript(deepseek_adapter.SCHEMA)
            for job_id, created_at in job_times.items():
                self.create_ledger_job(db, job_id=job_id, created_at=created_at,
                                       usage={"prompt_tokens": 1, "completion_tokens": 2})
        report = ao_usage_audit.audit(self.home, self.room, self.request_id, self.database)
        jobs = {item["job_sha256"] for item in report["api_delegates"]["jobs"]}
        self.assertEqual(jobs, {hashlib.sha256(job_id.encode()).hexdigest()
                                for job_id in ("at-start", "at-receipt-end")})

    def test_delegate_model_identifiers_are_validated_and_private(self):
        self.write_transcript([self.human()])
        self.build()
        ledger = self.home / "deepseek" / "ledger.sqlite3"
        ledger.parent.mkdir(parents=True)
        with sqlite3.connect(ledger) as db:
            db.executescript(deepseek_adapter.SCHEMA)
            self.create_ledger_job(db, job_id="invalid-observed-model",
                                   created_at="2026-01-01T00:00:30+00:00",
                                   usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3})
            db.execute("UPDATE jobs SET observed_model=? WHERE id=?",
                       ("leak text\nhere", "invalid-observed-model"))
        report = ao_usage_audit.audit(self.home, self.room, self.request_id, self.database)
        self.assertIsNone(report["api_delegates"]["jobs"][0]["observed_model"])
        self.assertIn("delegate_model_unavailable", report["api_delegates"]["reasons"])
        process = self.run_cli()
        self.assertEqual(process.returncode, 1, process.stderr)
        self.assertNotIn("leak text\nhere", process.stdout)

    def test_delegate_ledger_row_limit_returns_unavailable(self):
        self.write_transcript([self.human()])
        self.build()
        ledger = self.home / "deepseek" / "ledger.sqlite3"
        ledger.parent.mkdir(parents=True)
        with sqlite3.connect(ledger) as db:
            db.executescript(deepseek_adapter.SCHEMA)
            for index in range(4):
                self.create_ledger_job(db, job_id=f"bounded-job-{index}",
                                       created_at="2026-01-01T00:00:30+00:00",
                                       usage={"prompt_tokens": index + 1, "completion_tokens": 1})
        with patch.object(ao_usage_audit, "MAX_DELEGATE_ROWS", 3):
            report = ao_usage_audit.audit(self.home, self.room, self.request_id, self.database)
        self.assertEqual(report["api_delegates"],
                         {"coverage": "unavailable", "reason": "delegate_ledger_limit"})

    def test_oversized_delegate_usage_is_unavailable_without_parsing(self):
        self.write_transcript([self.human()])
        self.build()
        ledger = self.home / "deepseek" / "ledger.sqlite3"
        ledger.parent.mkdir(parents=True)
        with sqlite3.connect(ledger) as db:
            db.executescript(deepseek_adapter.SCHEMA)
            self.create_ledger_job(db, job_id="oversized-usage",
                                   created_at="2026-01-01T00:00:30+00:00", usage="x" * 65537)
        with patch.object(ao_usage_audit, "_job_usage",
                          side_effect=AssertionError("oversized delegate usage must not be parsed")) as parse_usage:
            report = ao_usage_audit.audit(self.home, self.room, self.request_id, self.database)
        parse_usage.assert_not_called()
        api = report["api_delegates"]
        self.assertEqual(api["coverage"], "incomplete")
        self.assertIn("delegate_usage_unavailable", api["reasons"])
        self.assertIsNone(api["jobs"][0]["usage"])

    def test_binding_refusal_has_unavailable_totals_and_skips_delegate_evaluation(self):
        self.report([self.human()])
        report = ao_usage_audit.audit(self.home, self.room, "unknown-request", self.database)
        self.assertEqual(report["coverage"], "unavailable")
        self.assertIsNone(report["parent"])
        self.assertEqual(report["native_totals"]["coverage"], "unavailable")
        self.assertEqual(report["api_delegates"],
                         {"coverage": "unavailable", "reason": "not_evaluated"})
        self.assertEqual(len(report["reasons"]), 1)

    def test_cli_closed_schema_exit_codes_and_privacy(self):
        response = self.assistant("assistant", "2026-01-01T00:00:20+00:00", "secret-message-id",
                                  {"input_tokens": 1, "output_tokens": 2,
                                   "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0})
        self.write_transcript([self.human(), response])
        self.build_without_delegate()
        command = [sys.executable, str(ROOT / "project_room.py"), "ao-native-usage-audit",
                   "--home", str(self.home), "--room", self.room, "--request", self.request_id,
                   "--ao-database", str(self.database)]
        process = subprocess.run(command, capture_output=True, text=True, timeout=300, env=self.environment())
        self.assertEqual(process.returncode, 0, process.stderr)
        report = json.loads(process.stdout)
        self.assertEqual(set(report), TOP_KEYS)
        self.assertEqual(report["version"], 1)
        self.assertEqual(report["kind"], "native_usage")
        self.assertEqual(report["scope"], "single_request")
        self.assertEqual(set(report["parent"]),
                         {"coverage", "cache_coverage", "configured_model", "attested_models", "responses",
                          "repeated_response_records", "synthetic_responses", "counters", "reasons"})
        self.assertEqual(set(report["parent"]["counters"]), USAGE_KEYS)
        for private in self.private_literals(("secret-message-id",)):
            self.assertNotIn(private, process.stdout)

        launch, completed = self.launch_records()
        self.write_transcript([self.human(), launch, completed])
        self.build_without_delegate()
        incomplete = subprocess.run(command, capture_output=True, text=True, timeout=300, env=self.environment())
        self.assertEqual(incomplete.returncode, 1, incomplete.stderr)

        invalid = command.copy()
        invalid[invalid.index(self.request_id)] = "../invalid"
        rejected = subprocess.run(invalid, capture_output=True, text=True, timeout=300, env=self.environment())
        self.assertEqual(rejected.returncode, 2)
        self.assertEqual(rejected.stdout, "")
        self.assertEqual(json.loads(rejected.stderr), {"error": "request_invalid"})


if __name__ == "__main__":
    unittest.main()
