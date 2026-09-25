"""Independent native interval and attribution counterexamples; no files or accounts."""
import datetime
import hashlib
import json
import unittest

import ao_evidence_audit_native as native
import ao_prompt_metrics
from test_ao_evidence_audit import AuditFixture, HISTORICAL_PATH, completed_result


def stamp(seconds):
    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).isoformat()


def scanner():
    limits = {"aggregate": 4096, "records": 1000, "record_ids": 1000, "tool_ids": 1000, "depth": 64}
    return native.RecordScanner(native.Collector(limits, set()), "a" * 64, "parent", None,
                                "native-session", "/workspace", "/evidence")


def put(scan, number, seconds, role, content, **changes):
    value = {"type": role, "uuid": "record-%d" % number, "timestamp": stamp(seconds),
             "sessionId": "native-session", "cwd": "/workspace", "isSidechain": False,
             "message": {"role": role, "content": content}}
    value.update(changes)
    scan.record(json.dumps(value).encode(), True, number)
    return value


def parent_interval(scan, **request_changes):
    request = {"text_sha256": hashlib.sha256(b"Continue.").hexdigest(), "created_at": 100.0}
    request.update(request_changes)
    return native.parent_interval(scan, request, {"observed_at": 150.0,
                                  "turn": {"state": "completed", "completedAt": stamp(140)}})


def completed(agent_id="child-1"):
    return {"status": "completed", "agentId": agent_id, "prompt": "bounded task", "content": [],
            "totalToolUseCount": 0, "totalDurationMs": 0, "totalTokens": 0,
            "usage": {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": None,
                      "cache_read_input_tokens": None, "server_tool_use": None,
                      "service_tier": None, "cache_creation": None}}


class NativeIndependentTests(unittest.TestCase):
    def test_read_selectors_and_result_shapes_are_type_preserving(self):
        base = {"file_path": "/evidence/a"}
        for extra in ({"offset": True}, {"offset": 0}, {"limit": None}, {"limit": 1.0},
                      {"pages": ""}, {"pages": False}, {"unknown": 1}):
            with self.subTest(extra=extra):
                self.assertIsNone(native.supported_read_input({**base, **extra}))
        for extra in ({}, {"offset": 1}, {"limit": 10}, {"pages": "1-3"}):
            self.assertIsNotNone(native.supported_read_input({**base, **extra}))
        for path, reason in (("relative", "path_relative"), ("/evidence/../other", "path_invalid"),
                             ("/evidence/\x00bad", "path_invalid"), ("/" + "x" * 16384, "path_bytes_limit")):
            value = native.classify_read({"file_path": path}, "/evidence")
            self.assertEqual(value["path_class"], "unclassified")
            self.assertEqual(value["reason"], reason)
        result = {"type": "tool_result", "tool_use_id": "read"}
        self.assertEqual(native.result_classification(result), "non_error")
        self.assertEqual(native.result_classification({**result, "is_error": True}), "error")
        self.assertEqual(native.result_classification({**result, "is_error": False}), "non_error")
        for extra in ({"is_error": None}, {"is_error": 1}, {"content": {"text": "bad"}},
                      {"content": [{"type": "unknown", "text": "bad"}]}):
            self.assertIsNone(native.canonical_result_block({**result, **extra}))

    def test_terminal_does_not_include_pre_anchor_timestamp(self):
        interval = native.Interval("terminal", 1, 100.0, None, 140.0)
        self.assertFalse(interval.contains(2, 50.0))
        self.assertTrue(interval.contains(2, 140.0))

    def test_child_record_numbers_are_not_parent_record_numbers(self):
        parent = native.Interval("boundary", 100, 100.0, 150, 140.0)
        child = parent.child(110.0, 130.0)
        self.assertTrue(child.covers_child(2, 115.0, 3, 120.0))
        grandchild = child.child(115.0, 120.0)
        self.assertTrue(grandchild.covers_child(1, 116.0, 2, 119.0))
        self.assertFalse(grandchild.covers_child(1, 109.0, 2, 119.0))

    def test_invalid_anchor_identity_never_authorizes_counts(self):
        for changes in ({"sessionId": "other"}, {"cwd": "/other"}, {"isSidechain": "false"},
                        {"message": {"role": "assistant", "content": "Continue."}}):
            with self.subTest(changes=changes):
                scan = scanner()
                put(scan, 1, 100, "user", "Continue.", origin={"kind": "human"}, **changes)
                self.assertIsNone(parent_interval(scan)[0])

    def test_malformed_later_human_blocks_terminal_fallback(self):
        for changes in ({"uuid": None}, {"timestamp": None}, {"isSidechain": "false"},
                        {"sessionId": "other"}, {"cwd": "/other"},
                        {"message": {"role": "user", "content": 17}},
                        {"message": {"role": "user", "content": [{"type": "text", "text": 17}]}}):
            with self.subTest(changes=changes):
                scan = scanner()
                put(scan, 1, 100, "user", "Continue.", origin={"kind": "human"})
                put(scan, 2, 120, "user", "Next", origin={"kind": "human"}, **changes)
                self.assertIsNone(parent_interval(scan)[0])

    def test_conflicting_anchor_uuid_cannot_remain_unique(self):
        scan = scanner()
        put(scan, 1, 100, "user", "Continue.", origin={"kind": "human"})
        put(scan, 2, 120, "user", "Other", origin={"kind": "human"}, uuid="record-1")
        self.assertIsNone(parent_interval(scan)[0])

    def test_compaction_summary_never_anchors(self):
        scan = scanner()
        put(scan, 1, 100, "user", "Continue.", origin={"kind": "human"}, isCompactSummary=True)
        self.assertIsNone(parent_interval(scan)[0])

    def test_huge_request_time_is_closed_refusal(self):
        scan = scanner()
        put(scan, 1, 100, "user", "Continue.", origin={"kind": "human"})
        self.assertIsNone(parent_interval(scan, created_at=10 ** 400)[0])

    def test_error_result_cannot_authorize_child_path(self):
        scan = scanner()
        put(scan, 1, 100, "user", "Continue.", origin={"kind": "human"})
        put(scan, 2, 110, "assistant", [{"type": "tool_use", "id": "launch", "name": "Agent",
                                       "input": {"prompt": "bounded task"}}])
        put(scan, 3, 120, "user", [{"type": "tool_result", "tool_use_id": "launch", "is_error": True}],
            toolUseResult=completed())
        candidates = native.launch_candidates(scan, parent_interval(scan)[0])
        self.assertEqual(len(candidates), 1)
        self.assertIsNone(candidates[0].agent_id)
        self.assertIsNone(candidates[0].interval)

    def test_null_error_flag_has_no_canonical_result(self):
        self.assertIsNone(native.canonical_result_block({"type": "tool_result", "tool_use_id": "launch",
                                                         "is_error": None}))

    def test_duplicate_after_human_boundary_is_not_counted(self):
        scan = scanner()
        put(scan, 1, 100, "user", "Continue.", origin={"kind": "human"})
        tool = {"type": "tool_use", "id": "read", "name": "Read", "input": {"file_path": "/evidence/a"}}
        put(scan, 2, 110, "assistant", [tool])
        put(scan, 3, 111, "user", [{"type": "tool_result", "tool_use_id": "read", "content": "ok"}])
        put(scan, 4, 120, "user", "Next", origin={"kind": "human"})
        put(scan, 5, 130, "assistant", [tool])
        counts = native.count_group(scan, parent_interval(scan)[0])[0]
        self.assertEqual(counts["read_requests"], 1)
        self.assertEqual(counts["duplicate_tool_observations"], 0)

    def test_conflicting_non_read_and_read_quarantines_in_both_orders(self):
        bash = {"type": "tool_use", "id": "conflict", "name": "Bash", "input": {"command": "true"}}
        read = {"type": "tool_use", "id": "conflict", "name": "Read", "input": {"file_path": "/evidence/a"}}
        for first, second in ((bash, read), (read, bash)):
            scan = scanner()
            put(scan, 1, 100, "user", "Continue.", origin={"kind": "human"})
            put(scan, 2, 110, "assistant", [first])
            put(scan, 3, 111, "assistant", [second])
            group = native.group_report(scan, parent_interval(scan)[0])
            self.assertEqual(group["counts"]["read_requests"], 0)
            self.assertEqual(group["counts"]["unsupported_tool_observations"], 1)
            self.assertNotEqual(group["coverage"], "complete")

    def test_nonobject_tool_input_cannot_claim_complete_source(self):
        scan = scanner()
        put(scan, 1, 100, "user", "Continue.", origin={"kind": "human"})
        put(scan, 2, 110, "assistant", [{"type": "tool_use", "id": "bad", "name": "Bash", "input": "bad"}])
        self.assertNotEqual(native.group_report(scan, parent_interval(scan)[0])["coverage"], "complete")


class HistoricalOracleTests(AuditFixture, unittest.TestCase):
    def test_original_prompt_byte_expectations(self):
        for case in json.loads(HISTORICAL_PATH.read_text())["prompt_cases"]:
            with self.subTest(case=case["name"]):
                assembly = ao_prompt_metrics.PromptAssembly("\n")
                for index, text in enumerate(case["components"]):
                    assembly.add("caller" if index == len(case["components"]) - 1 else "workflow", text)
                projection = assembly.projection("none")
                self.assertEqual(projection["total_bytes"], case["expected_packet_bytes"])
                self.assertEqual(projection["separator_bytes"], case["expected_separator_bytes"])

    def test_original_read_expectations_through_actual_cli(self):
        mappings = {"expected_distinct_read_calls": "read_requests", "expected_read_requests": "read_requests",
                    "expected_proven_Read_calls": "read_requests", "expected_duplicate_records": "duplicate_records",
                    "expected_successful_read_results": "reported_non_error_results",
                    "expected_failed_read_results": "reported_error_results",
                    "expected_unresolved_read_results": "unresolved_read_results"}
        for case in json.loads(HISTORICAL_PATH.read_text())["read_cases"]:
            self.reset()
            with self.subTest(case=case["name"]):
                records = [self.human()]
                name = case["name"]
                if name == "missing_child_file":
                    agent_id = case["launched_children"][0]
                    records.extend([
                        self.assistant_tools("launch", "2026-01-01T00:00:11+00:00",
                            [{"type": "tool_use", "id": "launch", "name": "Agent", "input": {"prompt": "Read."}}]),
                        self.user_results("launch-result", "2026-01-01T00:00:12+00:00",
                            [{"type": "tool_result", "tool_use_id": "launch", "content": "Completed."}],
                            toolUseResult=completed_result(agent_id, "Read."))])
                elif name == "bash_mentions_evidence_path":
                    records.append(self.assistant_tools("bash", "2026-01-01T00:00:11+00:00",
                        [{"type": "tool_use", "id": "bash", "name": case["tool_name"],
                          "input": {"command": case["command"]}}]))
                elif name == "privacy":
                    records.append(self.assistant_tools("private", "2026-01-01T00:00:11+00:00",
                                                        "\n".join(case["private_sentinels"])))
                elif name in ("duplicate_native_record", "conflicting_native_record"):
                    for record_id, tool_id in zip(case["record_uuids"], case["tool_ids"]):
                        records.append(self.assistant_tools(record_id, "2026-01-01T00:00:11+00:00",
                            [{"type": "tool_use", "id": tool_id, "name": "Read",
                              "input": {"file_path": self.target()}}]))
                    records.append(self.user_results("result", "2026-01-01T00:00:12+00:00",
                        [{"type": "tool_result", "tool_use_id": "r1", "content": "Read result."}]))
                else:
                    calls = case["tool_calls"] if "tool_calls" in case else [{"id": case["tool_id"], "name": "Read"}]
                    for index, call in enumerate(calls):
                        value = {"file_path": self.target()}
                        value.update({key: call[key] for key in ("offset", "limit") if key in call})
                        records.append(self.assistant_tools("read-" + str(index), "2026-01-01T00:00:11+00:00",
                            [{"type": "tool_use", "id": call["id"], "name": call["name"], "input": value}]))
                        if case.get("tool_result_present", True):
                            block = {"type": "tool_result", "tool_use_id": call["id"], "content": "Read result."}
                            if "result_is_error" in case:
                                block["is_error"] = case["result_is_error"]
                            records.append(self.user_results("result-" + str(index), "2026-01-01T00:00:12+00:00", [block]))
                records.append(self.human(uuid="next", timestamp="2026-01-01T00:01:00+00:00", text="Next."))
                self.write_transcript(records)
                self.build()
                report = self.report()
                for expected, field in mappings.items():
                    if expected in case:
                        self.assertEqual(report["parent"]["counts"][field], case[expected])
                if "expected_coverage" in case:
                    self.assertEqual(report["coverage"], case["expected_coverage"])
                if "expected_redundant_read_verdict" in case:
                    self.assertEqual(report["redundant_read_verdict"], case["expected_redundant_read_verdict"])
                if "expected_child_read_count" in case:
                    self.assertIsNone(self.expect_actor(report, case["launched_children"][0])["observation"])
                if "expected_coverage_note" in case:
                    self.assertIn("other_tool_access_not_counted", report["limitations"])
                for sentinel in case.get("private_sentinels", []):
                    self.assertNotIn(sentinel, json.dumps(report))


class PreservedDiagnosticCases(AuditFixture, unittest.TestCase):
    def test_anchor_window_ties_and_ambiguity_block_attribution(self):
        with self.subTest("unique matching text outside the controller window"):
            self.reset()
            self.write_transcript([self.human(timestamp="2026-01-01T00:01:11+00:00"),
                                   self.assistant_tools("u-read", "2026-01-01T00:01:12+00:00",
                                                        [{"type": "tool_use", "id": "r1", "name": "Read",
                                                          "input": {"file_path": str(self.evidence / "a.txt")}}]),
                                   self.human(uuid="human-2", timestamp="2026-01-01T00:02:00+00:00",
                                              text="Next.")])
            self.build()
            report = self.report()
            self.assertEqual(report["coverage"], "incomplete")
            self.assertIn("interval_unbound", report["reasons"])
            self.assertTrue(all(value is None for value in report["parent"]["counts"].values()))
        with self.subTest("two matching anchors inside the window"):
            self.reset()
            self.write_transcript([self.human(),
                                   self.human(uuid="human-twin", timestamp="2026-01-01T00:00:20+00:00"),
                                   self.human(uuid="human-2", timestamp="2026-01-01T00:01:00+00:00",
                                              text="Next.")])
            self.build()
            report = self.report()
            self.assertEqual(report["coverage"], "incomplete")
            self.assertIn("interval_ambiguous", report["reasons"])
        with self.subTest("tied endpoint humans"):
            self.reset()
            self.write_transcript([self.human(),
                                   self.human(uuid="human-tie-1", timestamp="2026-01-01T00:01:00+00:00",
                                              text="Next."),
                                   self.human(uuid="human-tie-2", timestamp="2026-01-01T00:01:00+00:00",
                                              text="Also next.")])
            self.build()
            report = self.report()
            self.assertEqual(report["coverage"], "incomplete")
            self.assertIn("interval_ambiguous", report["reasons"])


    def test_actor_collision_defeats_child_attribution(self):
        target = str(self.evidence / "a.txt")
        self.write_transcript([self.human(),
                               self.assistant_tools("u-launch", "2026-01-01T00:00:11+00:00",
                                                    [{"type": "tool_use", "id": "t1", "name": "Agent",
                                                      "input": {"prompt": "Read bounded evidence."}}]),
                               self.user_results("u-launch-result", "2026-01-01T00:00:15+00:00",
                                                 [{"type": "tool_result", "tool_use_id": "t1",
                                                   "content": "bounded result"}],
                                                 toolUseResult=completed_result("a1", "Read bounded evidence.")),
                               self.human(uuid="human-2", timestamp="2026-01-01T00:01:00+00:00", text="Next.")])
        self.write_child("a1", [self.child_record("assistant", "c-launch", "2026-01-01T00:00:12+00:00",
                                                  [{"type": "tool_use", "id": "c2", "name": "Agent",
                                                    "input": {"prompt": "Read bounded evidence."}}]),
                                self.child_record("user", "c-launch-result", "2026-01-01T00:00:13+00:00",
                                                  [{"type": "tool_result", "tool_use_id": "c2",
                                                    "content": "bounded result"}],
                                                  toolUseResult=completed_result("a1", "Read bounded evidence."))])
        self.build()
        report = self.report()
        self.assertEqual(report["coverage"], "incomplete")
        self.assertIn("child_attribution_ambiguous", report["reasons"])
        self.assertEqual(report["child_coverage"], "incomplete")
        for entry in report["children"]:
            self.assertIsNone(entry["observation"])
        self.assertNotIn('"a1"', json.dumps(report))


    def test_unsupported_evidence_reports_honest_unknown(self):
        with self.subTest("preparation without a configuration root"):
            self.reset()
            self.write_transcript([self.human()])
            self.build(preparation_overrides={"routing": {"version": 2, "claude_config_dir": None}})
            report = self.report()
            self.assertEqual(report["coverage"], "unavailable")
            self.assertIn("preparation_unbound", report["reasons"])
            self.assertEqual(report["sources"], [])
        with self.subTest("non-Claude request never opens a Claude transcript"):
            self.reset()
            self.build(harness="codex")
            report = self.report()
            self.assertEqual(report["coverage"], "unavailable")
            self.assertEqual(report["reasons"], ["unsupported_harness"])
        with self.subTest("completed projection with a missing required usage member"):
            self.reset()
            broken = completed_result("a1", "Read bounded evidence.")
            del broken["usage"]["server_tool_use"]
            self.write_transcript([self.human(),
                                   self.assistant_tools("u-launch", "2026-01-01T00:00:11+00:00",
                                                        [{"type": "tool_use", "id": "t1", "name": "Agent",
                                                          "input": {"prompt": "Read bounded evidence."}}]),
                                   self.user_results("u-launch-result", "2026-01-01T00:00:15+00:00",
                                                     [{"type": "tool_result", "tool_use_id": "t1",
                                                       "content": "bounded result"}], toolUseResult=broken),
                                   self.human(uuid="human-2", timestamp="2026-01-01T00:01:00+00:00",
                                              text="Next.")])
            self.build()
            report = self.report()
            self.assertEqual(report["coverage"], "incomplete")
            self.assertIn("child_interval_unbound", report["reasons"])
            self.assertIsNone(self.expect_actor(report, "a1")["observation"])
        with self.subTest("unknown request projection version is never guessed"):
            self.reset()
            self.write_transcript([self.human()])
            self.build()
            state = json.loads((self.home / "ao" / "rooms" / self.room / "state.json").read_text("utf-8"))
            state["requests"][self.request_id]["harness"] = "some-future-harness"
            (self.home / "ao" / "rooms" / self.room / "state.json").write_text(json.dumps(state), encoding="utf-8")
            report = self.report()
            self.assertEqual(report["coverage"], "unavailable")
            self.assertEqual(report["reasons"], ["unsupported_harness"])
