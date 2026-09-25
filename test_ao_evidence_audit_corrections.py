"""Focused regressions for the sixteen independently identified #58 findings (D58-I1..I8, D58-N1..N8).

Every expectation comes from the exact specification and the root probes' concrete
counterexamples, never from the unaccepted draft. Real CLI/binding/SQLite/source boundaries
are exercised; no Service, model, network, subprocess collector, state repair or credential read.
"""

import json
import os
from pathlib import Path
import shutil
import sqlite3
import unittest
from unittest import mock

import ao_evidence_audit
import ao_evidence_audit_io
import ao_evidence_audit_native as native
from test_ao_evidence_audit import AO_SESSION_ID, AuditFixture, CREATED_AT, NATIVE_UUID, canon, completed_result, sha


class CorrectionTests(AuditFixture, unittest.TestCase):
    # ---- D58-N1: terminal interval lower bound -------------------------------------------------

    def test_terminal_interval_lower_bound(self):
        terminal = native.Interval("terminal", 1, 100.0, None, 140.0)
        self.assertFalse(terminal.contains(2, 50.0))
        self.assertFalse(terminal.contains(2, 99.999))
        self.assertTrue(terminal.contains(2, 100.0))
        self.assertTrue(terminal.contains(2, 140.0))
        self.assertFalse(terminal.contains(1, 120.0))

    def test_terminal_interval_excludes_pre_anchor_events_end_to_end(self):
        self.write_transcript([
            self.human(),
            self.assistant_tools("early-read", "2026-01-01T00:00:05+00:00",
                                 [{"type": "tool_use", "id": "r1", "name": "Read",
                                   "input": {"file_path": self.target()}}])])
        self.build(turn_state="completed", completed_at="2026-01-01T00:00:40+00:00", observed_at=CREATED_AT + 40)
        report = self.report()
        self.assertEqual(report["coverage"], "complete")
        self.assertEqual(report["parent"]["interval_coverage"], "complete")
        self.assertEqual(report["parent"]["counts"]["read_requests"], 0)

    # ---- D58-N2: recursive child intervals -----------------------------------------------------

    def recursive_children(self, grandchild_completion="2026-01-01T00:00:18+00:00"):
        self.write_transcript([
            self.human(),
            self.assistant_tools("launch-1", "2026-01-01T00:00:11+00:00",
                                 [{"type": "tool_use", "id": "t1", "name": "Agent",
                                   "input": {"prompt": "Read bounded evidence."}}]),
            self.user_results("launch-result-1", "2026-01-01T00:00:20+00:00",
                              [{"type": "tool_result", "tool_use_id": "t1", "content": "bounded result"}],
                              toolUseResult=completed_result("a1", "Read bounded evidence.")),
            self.human(uuid="human-2", timestamp="2026-01-01T00:01:00+00:00", text="Next.")])
        self.write_child("a1", [
            self.child_record("assistant", "child-launch", "2026-01-01T00:00:12+00:00",
                              [{"type": "tool_use", "id": "t2", "name": "Agent",
                                "input": {"prompt": "Read bounded evidence."}}]),
            self.child_record("user", "child-launch-result", grandchild_completion,
                              [{"type": "tool_result", "tool_use_id": "t2", "content": "bounded result"}],
                              toolUseResult=completed_result("a2", "Read bounded evidence."))])
        self.write_child("a2", [
            self.child_record("assistant", "grandchild-read", "2026-01-01T00:00:14+00:00",
                              [{"type": "tool_use", "id": "k1", "name": "Read",
                                "input": {"file_path": self.target()}}], agent="a2"),
            self.child_record("user", "grandchild-result", "2026-01-01T00:00:15+00:00",
                              [{"type": "tool_result", "tool_use_id": "k1", "content": "bounded result"}],
                              agent="a2")])
        self.build()
        return self.report()

    def test_recursive_child_intervals(self):
        report = self.recursive_children()
        self.assertEqual(report["coverage"], "complete")
        self.assertEqual(len(report["children"]), 2)
        self.assertEqual(report["child_coverage"], "complete")
        observation = self.expect_actor(report, "a2")["observation"]
        self.assertEqual(observation["counts"]["read_requests"], 1)
        self.assertEqual(observation["counts"]["evidence_root_requests"], 1)

    def test_recursive_child_outside_child_interval_is_unbound(self):
        report = self.recursive_children(grandchild_completion="2026-01-01T00:00:25+00:00")
        self.assertNotEqual(report["coverage"], "complete")
        self.assertIn("launch_unresolved", report["reasons"])
        self.assertEqual(len(report["children"]), 1)
        self.assertIsNotNone(self.expect_actor(report, "a1")["observation"])
        self.assertNotEqual(report["child_coverage"], "complete")

    # ---- D58-N3: malformed potentially applicable records --------------------------------------

    def test_malformed_tool_input_prevents_complete_source(self):
        self.write_transcript([
            self.human(),
            self.assistant_tools("bad-tool", "2026-01-01T00:00:11+00:00",
                                 [{"type": "tool_use", "id": "bad", "name": "Bash", "input": "not-an-object"}]),
            self.human(uuid="human-2", timestamp="2026-01-01T00:01:00+00:00", text="Next.")])
        self.build()
        report = self.report()
        self.assertEqual(report["coverage"], "incomplete")
        self.assertIn("source_malformed", report["reasons"])
        self.assertEqual(report["parent"]["source_coverage"], "incomplete")

    # ---- D58-N4: interval-scoped duplicate counts ----------------------------------------------

    def test_duplicate_tool_observation_after_exclusive_boundary_is_excluded(self):
        self.write_transcript([
            self.human(),
            self.assistant_tools("read-1", "2026-01-01T00:00:11+00:00",
                                 [{"type": "tool_use", "id": "r1", "name": "Read",
                                   "input": {"file_path": self.target()}}]),
            self.user_results("result-1", "2026-01-01T00:00:12+00:00",
                              [{"type": "tool_result", "tool_use_id": "r1", "content": "bounded result"}]),
            self.human(uuid="human-2", timestamp="2026-01-01T00:01:00+00:00", text="Next."),
            self.assistant_tools("read-later", "2026-01-01T00:01:01+00:00",
                                 [{"type": "tool_use", "id": "r1", "name": "Read",
                                   "input": {"file_path": self.target()}}])])
        self.build()
        report = self.report()
        self.assertEqual(report["coverage"], "complete")
        counts = report["parent"]["counts"]
        self.assertEqual(counts["duplicate_tool_observations"], 0)
        self.assertEqual(counts["read_requests"], 1)
        self.assertEqual(counts["repeated_identical_inputs"], 0)

    # ---- D58-N5: later potentially genuine humans block the terminal rule -----------------------

    def test_later_malformed_human_blocks_terminal_fallback(self):
        self.write_transcript([
            self.human(),
            self.parent_record("user", "human-malformed", "2026-01-01T00:00:20+00:00", "Another request.",
                               origin={"kind": "human"}, uuid=None),
            self.assistant_tools("too-late-read", "2026-01-01T00:00:25+00:00",
                                 [{"type": "tool_use", "id": "late", "name": "Read",
                                   "input": {"file_path": self.target()}}]),
            self.user_results("too-late-result", "2026-01-01T00:00:26+00:00",
                              [{"type": "tool_result", "tool_use_id": "late", "content": "bounded result"}])])
        self.build(turn_state="completed", completed_at="2026-01-01T00:00:30+00:00", observed_at=CREATED_AT + 40)
        report = self.report()
        self.assertEqual(report["coverage"], "incomplete")
        self.assertIn("interval_unbound", report["reasons"])
        self.assertNotEqual(report["parent"]["interval_coverage"], "complete")
        self.assertTrue(report["parent"]["counts"] is None
                        or report["parent"]["counts"]["read_requests"] in (None, 0))

    def test_later_human_with_invalid_timestamp_blocks_terminal_fallback(self):
        self.write_transcript([
            self.human(),
            self.parent_record("user", "human-invalid", "INVALID_BOUNDARY_SENTINEL", "Another request.",
                               origin={"kind": "human"}),
            self.assistant_tools("too-late-read", "2026-01-01T00:00:25+00:00",
                                 [{"type": "tool_use", "id": "late", "name": "Read",
                                   "input": {"file_path": self.target()}}])])
        self.build(turn_state="completed", completed_at="2026-01-01T00:00:30+00:00", observed_at=CREATED_AT + 40)
        report = self.report()
        self.assertEqual(report["coverage"], "incomplete")
        self.assertIn("interval_unbound", report["reasons"])

    # ---- D58-N6: compaction summaries are never anchors ----------------------------------------

    def test_compaction_summary_is_not_an_anchor(self):
        summary = self.parent_record("user", "compact-1", "2026-01-01T00:00:10+00:00", "Continue.",
                                     origin={"kind": "human"}, isCompactSummary=True,
                                     isVisibleInTranscriptOnly=True)
        self.write_transcript([summary])
        self.build()
        report = self.report()
        self.assertEqual(report["coverage"], "incomplete")
        self.assertIn("interval_unbound", report["reasons"])
        self.assertTrue(report["parent"]["counts"] is None
                        or report["parent"]["counts"]["read_requests"] in (None, 0))

    # ---- D58-N7: symmetric Read-ID conflict quarantine ------------------------------------------

    def conflicting_identity_records(self, read_first=False):
        bash = {"type": "tool_use", "id": "conflict", "name": "Bash", "input": {"command": "true"}}
        read = {"type": "tool_use", "id": "conflict", "name": "Read", "input": {"file_path": self.target()}}
        first, second = (read, bash) if read_first else (bash, read)
        return [self.human(),
                self.assistant_tools("tool-1", "2026-01-01T00:00:11+00:00", [first]),
                self.assistant_tools("tool-2", "2026-01-01T00:00:12+00:00", [second]),
                self.human(uuid="human-2", timestamp="2026-01-01T00:01:00+00:00", text="Next.")]

    def test_conflicting_read_identity_is_quarantined_symmetrically(self):
        for read_first in (False, True):
            with self.subTest(read_first=read_first):
                self.reset()
                self.write_transcript(self.conflicting_identity_records(read_first))
                self.build()
                report = self.report()
                counts = report["parent"]["counts"]
                self.assertEqual(report["coverage"], "incomplete")
                self.assertIn("identity_conflict", report["reasons"])
                self.assertEqual(counts["unsupported_tool_observations"], 1)
                self.assertEqual(counts["read_requests"], 0)
                self.assertEqual(counts["evidence_root_requests"], 0)
                self.assertEqual(counts["distinct_slices"], 0)

    def test_conflicting_non_read_identity_stays_outside_the_counter(self):
        self.write_transcript([
            self.human(),
            self.assistant_tools("bash-1", "2026-01-01T00:00:11+00:00",
                                 [{"type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": "a"}}]),
            self.assistant_tools("bash-2", "2026-01-01T00:00:12+00:00",
                                 [{"type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": "b"}}]),
            self.human(uuid="human-2", timestamp="2026-01-01T00:01:00+00:00", text="Next.")])
        self.build()
        report = self.report()
        self.assertEqual(report["coverage"], "complete")
        self.assertEqual(report["parent"]["counts"]["unsupported_tool_observations"], 0)

    # ---- D58-N8: malformed result association ---------------------------------------------------

    def test_malformed_is_error_is_not_a_launch_association(self):
        self.assertIsNone(native.canonical_result_block({"type": "tool_result", "tool_use_id": "launch",
                                                         "is_error": None}))
        self.write_transcript([
            self.human(),
            self.assistant_tools("launch-1", "2026-01-01T00:00:11+00:00",
                                 [{"type": "tool_use", "id": "t1", "name": "Agent",
                                   "input": {"prompt": "Read bounded evidence."}}]),
            self.user_results("launch-result-1", "2026-01-01T00:00:15+00:00",
                              [{"type": "tool_result", "tool_use_id": "t1", "content": "bounded result",
                                "is_error": None}],
                              toolUseResult=completed_result("a1", "Read bounded evidence.")),
            self.human(uuid="human-2", timestamp="2026-01-01T00:01:00+00:00", text="Next.")])
        self.write_child("a1", [self.child_record("assistant", "child-read", "2026-01-01T00:00:12+00:00",
                                                  [{"type": "tool_use", "id": "k1", "name": "Read",
                                                    "input": {"file_path": self.target()}}])])
        self.build()
        report = self.report()
        self.assertNotEqual(report["coverage"], "complete")
        self.assertIn("launch_unresolved", report["reasons"])
        self.assertEqual(report["children"], [])

    # ---- D58-I1: escaped SQLite URI and closed connection ----------------------------------------

    def test_sqlite_uri_is_escaped_and_read_only(self):
        self.write_transcript([self.human()])
        self.build()
        weird = self.base / "ao?mode=rwc&ignored.db"
        shutil.copyfile(self.database, weird)
        decoy = self.base / "ao"
        shutil.copyfile(self.database, decoy)
        with sqlite3.connect(decoy) as db:
            db.execute("UPDATE sessions SET controller_generation='decoy-generation'")
        report = ao_evidence_audit.audit(self.home, self.room, self.request_id, weird, self.evidence)
        self.assertEqual(report["coverage"], "complete")
        self.assertEqual(report["owner_sha256"], self.owner_sha256())
        missing = self.base / "absent?mode=rwc&ignored.db"
        report = ao_evidence_audit.audit(self.home, self.room, self.request_id, missing, self.evidence)
        self.assertEqual(report["reasons"], ["owner_unavailable"])
        self.assertFalse((self.base / "absent").exists())
        self.assertFalse(missing.exists())

    def test_owner_connection_closes_before_transcript_streaming(self):
        self.write_transcript([self.human()])
        self.build()
        events = []
        real_connect = sqlite3.connect
        original_component = ao_evidence_audit_io._open_component

        class Wrapped:
            def __init__(self, *args, **kwargs):
                events.append("connect")
                self.connection = real_connect(*args, **kwargs)

            def __getattr__(self, name):
                return getattr(self.connection, name)

            @property
            def row_factory(self):
                return self.connection.row_factory

            @row_factory.setter
            def row_factory(self, value):
                self.connection.row_factory = value

            def close(self):
                events.append("close")
                self.connection.close()

        def component(name, flags, dir_fd):
            if name == NATIVE_UUID + ".jsonl":
                events.append("transcript")
            return original_component(name, flags, dir_fd)

        with mock.patch.object(ao_evidence_audit.sqlite3, "connect", Wrapped), \
                mock.patch.object(ao_evidence_audit_io, "_open_component", component):
            report = self.audit_in_process()
        self.assertEqual(report["coverage"], "complete")
        self.assertEqual(events[:2], ["connect", "close"])
        self.assertLess(events.index("close"), events.index("transcript"))
        self.assertEqual(events.count("connect"), 2)
        self.assertEqual(events.count("close"), 2)
        self.assertLess(events.index("transcript"), events.index("close", events.index("close") + 1))

    # ---- D58-I5: charged I/O across rejected scans and both passes ------------------------------

    def scan_file(self, collector, name, limits, record=None):
        opened = self.source_root.open_file((name,), native.audit_io.MAX_TRANSCRIPT_BYTES, "source_missing",
                                            "source_unsafe", "transcript_bytes_limit")
        return native.audit_io.scan_source(opened, record or (lambda payload, terminated, number: None), collector,
                                           limits)

    def test_rejected_streams_charge_actual_bytes_and_never_read_past_budget(self):
        streams = self.base / "streams"
        streams.mkdir()
        for name in ("a", "b", "c"):
            (streams / name).write_bytes(b"1234567")
        self.source_root = native.audit_io.Root(str(streams), "source_missing", "source_unsafe")
        limits = {"aggregate": 10, "record": 1_000_000, "records": 100, "record_ids": 100, "tool_ids": 100,
                  "depth": 64}
        collector = native.Collector(limits, set())
        failures = []
        for name in ("a", "b", "c"):
            try:
                self.scan_file(collector, name, limits)
            except native.audit_io.SourceError as exc:
                failures.append(exc.reason)
        self.assertEqual(failures, ["aggregate_bytes_limit", "aggregate_bytes_limit"])
        self.assertEqual(collector.aggregate, 7)
        self.source_root.close()

    def test_late_rejected_source_charges_the_bytes_it_read(self):
        streams = self.base / "late"
        streams.mkdir()
        (streams / "a").write_bytes(b"0123456789ABCDEFGHIJKLM")
        self.source_root = native.audit_io.Root(str(streams), "source_missing", "source_unsafe")
        limits = {"aggregate": 1_000, "record": 10, "records": 100, "record_ids": 100, "tool_ids": 100,
                  "depth": 64}
        collector = native.Collector(limits, set())
        observed = []
        original = self.source_root.open_file

        def tracked(*args, **kwargs):
            opened = original(*args, **kwargs)
            handle = opened.handle

            class ReadSpy:
                def __getattr__(self, name):
                    return getattr(handle, name)

                def read(inner, size):
                    before = os.lseek(handle.fileno(), 0, os.SEEK_CUR)
                    value = handle.read(size)
                    observed.append(len(value))
                    self.assertEqual(os.lseek(handle.fileno(), 0, os.SEEK_CUR) - before, len(value))
                    return value

            opened.handle = ReadSpy()
            return opened

        with mock.patch.object(self.source_root, "open_file", tracked):
            with self.assertRaises(native.audit_io.SourceError) as raised:
                self.scan_file(collector, "a", limits)
        self.assertEqual(raised.exception.reason, "record_bytes_limit")
        self.assertEqual(collector.aggregate, sum(observed))
        self.assertGreater(collector.aggregate, limits["record"])
        self.assertLessEqual(collector.aggregate, 23)
        self.source_root.close()

    def test_charged_high_water_never_exceeds_the_aggregate_bound(self):
        self.write_transcript([self.human(),
                               self.assistant_tools("u-read", "2026-01-01T00:00:11+00:00",
                                                    [{"type": "tool_use", "id": "r1", "name": "Read",
                                                      "input": {"file_path": self.target()}}]),
                               self.user_results("u-result", "2026-01-01T00:00:12+00:00",
                                                 [{"type": "tool_result", "tool_use_id": "r1",
                                                   "content": "bounded result"}]),
                               self.human(uuid="human-2", timestamp="2026-01-01T00:01:00+00:00", text="Next.")])
        self.build()
        instances = []

        class Spy(native.Collector):
            def __init__(self, limits, notes):
                super().__init__(limits, notes)
                self.high_water = 0
                instances.append(self)

            def charge(self, count):
                super().charge(count)
                self.high_water = max(self.high_water, self.aggregate)

            def spend(self, count):
                super().spend(count)
                self.high_water = max(self.high_water, self.aggregate)

        limit = 12
        with mock.patch.object(native, "Collector", Spy), \
                mock.patch.object(ao_evidence_audit_io, "MAX_AGGREGATE_BYTES", limit):
            report = self.audit_in_process()
        self.assertNotEqual(report["coverage"], "complete")
        self.assertEqual(len(instances), 1)
        self.assertLessEqual(instances[0].high_water, limit)

    # ---- D58-I6: second-pass stability ----------------------------------------------------------

    def test_second_pass_post_read_metadata_change_is_refused(self):
        streams = self.base / "stable"
        streams.mkdir()
        path = streams / "a"
        path.write_bytes(b"bounded payload\n")
        root = native.audit_io.Root(str(streams), "source_missing", "source_unsafe")
        limits = {"aggregate": 100_000, "record": 1_000, "records": 100, "record_ids": 100, "tool_ids": 100,
                  "depth": 64}
        collector = native.Collector(limits, set())
        opened = root.open_file(("a",), native.audit_io.MAX_TRANSCRIPT_BYTES, "source_missing", "source_unsafe",
                                "transcript_bytes_limit")
        result = native.audit_io.scan_source(opened, lambda payload, terminated, number: None, collector, limits)
        collector.new_pass()
        opened = root.open_file(("a",), native.audit_io.MAX_TRANSCRIPT_BYTES, "source_missing", "source_unsafe",
                                "transcript_bytes_limit")
        real_handle = opened.handle
        stat = path.stat()

        class Proxy:
            def read(self, size=-1):
                data = real_handle.read(size)
                os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
                return data

            def fileno(self):
                return real_handle.fileno()

            def close(self):
                real_handle.close()

        opened.handle = Proxy()
        with self.assertRaises(native.audit_io.SourceError) as raised:
            native.audit_io.rehash_source(opened, result, collector, limits)
        self.assertEqual(raised.exception.reason, "source_changed")
        root.close()

    def test_late_child_listing_growth_is_incomplete(self):
        target = self.target()
        self.write_transcript([self.human(),
                               self.assistant_tools("u-launch", "2026-01-01T00:00:11+00:00",
                                                    [{"type": "tool_use", "id": "t1", "name": "Agent",
                                                      "input": {"prompt": "Read bounded evidence."}}]),
                               self.user_results("u-launch-result", "2026-01-01T00:00:15+00:00",
                                                 [{"type": "tool_result", "tool_use_id": "t1",
                                                   "content": "bounded result"}],
                                                 toolUseResult=completed_result("a1", "Read bounded evidence.")),
                               self.human(uuid="human-2", timestamp="2026-01-01T00:01:00+00:00", text="Next.")])
        self.write_child("a1", [self.child_record("assistant", "c-read", "2026-01-01T00:00:12+00:00",
                                                  [{"type": "tool_use", "id": "k1", "name": "Read",
                                                    "input": {"file_path": target}}]),
                                self.child_record("user", "c-result", "2026-01-01T00:00:13+00:00",
                                                  [{"type": "tool_result", "tool_use_id": "k1",
                                                    "content": "bounded result"}])])
        self.build()
        self.assertEqual(self.audit_in_process()["coverage"], "complete")
        original = ao_evidence_audit_io._open_component
        state = {"done": False}

        def listing(name, flags, dir_fd):
            if name == "agent-a1.jsonl" and not state["done"]:
                state["done"] = True
                (self.subagents / "agent-late.jsonl").write_text("", encoding="utf-8")
            return original(name, flags, dir_fd)

        with mock.patch.object(ao_evidence_audit_io, "_open_component", listing):
            report = self.audit_in_process()
        self.assertIn("source_changed", report["reasons"])
        self.assertNotEqual(report["coverage"], "complete")

    def test_grown_earlier_source_is_incomplete(self):
        target = self.target()
        self.write_transcript([self.human(),
                               self.assistant_tools("u-launch", "2026-01-01T00:00:11+00:00",
                                                    [{"type": "tool_use", "id": "t1", "name": "Agent",
                                                      "input": {"prompt": "Read bounded evidence."}}]),
                               self.user_results("u-launch-result", "2026-01-01T00:00:15+00:00",
                                                 [{"type": "tool_result", "tool_use_id": "t1",
                                                   "content": "bounded result"}],
                                                 toolUseResult=completed_result("a1", "Read bounded evidence.")),
                               self.human(uuid="human-2", timestamp="2026-01-01T00:01:00+00:00", text="Next.")])
        self.write_child("a1", [self.child_record("assistant", "c-read", "2026-01-01T00:00:12+00:00",
                                                  [{"type": "tool_use", "id": "k1", "name": "Read",
                                                    "input": {"file_path": target}}])])
        self.build()
        original = ao_evidence_audit_io._open_component
        state = {"done": False}

        def racing(name, flags, dir_fd):
            if name == "agent-a1.jsonl" and not state["done"]:
                state["done"] = True
                with self.transcript.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(self.parent_record("assistant", "late-read",
                                                               "2026-01-01T00:00:14+00:00",
                                                               [{"type": "tool_use", "id": "r9",
                                                                 "name": "Read",
                                                                 "input": {"file_path": target}}])) + "\n")
            return original(name, flags, dir_fd)

        with mock.patch.object(ao_evidence_audit_io, "_open_component", racing):
            report = self.audit_in_process()
        self.assertIn("source_changed", report["reasons"])
        self.assertNotEqual(report["coverage"], "complete")

    # ---- D58-I7: descriptor ownership, roots, names and stability -------------------------------

    def test_root_open_refuses_symlink_and_recheck_rejects_swaps(self):
        real = self.base / "real"
        other = self.base / "other-root"
        real.mkdir()
        other.mkdir()
        link = self.base / "link-root"
        link.symlink_to(real)
        with self.assertRaises(ao_evidence_audit_io.SourceError) as raised:
            ao_evidence_audit_io.Root(str(link), "missing", "unsafe")
        self.assertEqual(raised.exception.reason, "unsafe")
        root = ao_evidence_audit_io.Root(str(real), "missing", "unsafe")
        renamed = self.base / "real-moved"
        real.rename(renamed)
        real.symlink_to(renamed)
        with self.assertRaises(ao_evidence_audit_io.SourceError) as raised:
            root.recheck_identity("changed")
        self.assertEqual(raised.exception.reason, "changed")
        root.close()
        root = ao_evidence_audit_io.Root(str(renamed), "missing", "unsafe")
        real.unlink()
        renamed.rename(real)
        real.rmdir()
        real.symlink_to(other)
        with self.assertRaises(ao_evidence_audit_io.SourceError):
            root.recheck_identity("changed")
        root.close()

    def test_room_root_replacement_is_refused(self):
        self.write_transcript([self.human()])
        self.build()
        room = self.home / "ao" / "rooms" / self.room
        original = ao_evidence_audit_io._open_component
        state = {"done": False}

        def swapping(name, flags, dir_fd):
            if name == "state.json" and not state["done"]:
                state["done"] = True
                moved = room.with_name(self.room + "-moved")
                room.rename(moved)
                room.symlink_to(moved)
            return original(name, flags, dir_fd)

        with mock.patch.object(ao_evidence_audit_io, "_open_component", swapping):
            report = self.audit_in_process()
        self.assertEqual(report["coverage"], "unavailable")
        self.assertEqual(report["reasons"], ["request_integrity"])

    def test_config_root_replacement_is_refused(self):
        self.write_transcript([self.human()])
        self.build()
        original = ao_evidence_audit_io._open_component
        state = {"count": 0}

        def swapping(name, flags, dir_fd):
            if name == NATIVE_UUID + ".jsonl":
                state["count"] += 1
                if state["count"] == 2:
                    moved = self.config_root.with_name("claude-config-moved")
                    self.config_root.rename(moved)
                    self.config_root.symlink_to(moved)
            return original(name, flags, dir_fd)

        with mock.patch.object(ao_evidence_audit_io, "_open_component", swapping):
            report = self.audit_in_process()
        self.assertNotEqual(report["coverage"], "complete")
        self.assertTrue(set(report["reasons"]) & {"source_changed", "source_unsafe", "source_missing"})

    def test_database_replacement_is_owner_conflict(self):
        self.write_transcript([self.human()])
        self.build()
        original = ao_evidence_audit_io._open_component
        state = {"done": False}

        def swapping(name, flags, dir_fd):
            if name == NATIVE_UUID + ".jsonl" and not state["done"]:
                state["done"] = True
                with sqlite3.connect(self.database) as db:
                    db.execute("UPDATE sessions SET controller_generation='generation-after'")
            return original(name, flags, dir_fd)

        with mock.patch.object(ao_evidence_audit_io, "_open_component", swapping):
            report = self.audit_in_process()
        self.assertIn("owner_conflict", report["reasons"])
        self.assertNotEqual(report["coverage"], "complete")

    def test_foreign_owned_source_is_refused(self):
        self.write_transcript([self.human()])
        self.build()
        info = self.config_root.stat()
        target = (info.st_dev, info.st_ino)
        original = ao_evidence_audit_io._owned_directory
        checked = []

        def foreign_source(info):
            if (info.st_dev, info.st_ino) == target:
                checked.append(target)
                return False
            return original(info)

        with mock.patch.object(ao_evidence_audit_io, "_owned_directory", foreign_source):
            report = self.audit_in_process()
        self.assertTrue(checked)
        self.assertIn("source_unsafe", report["reasons"])
        self.assertEqual(report["coverage"], "unavailable")

    # ---- D58-I4: request/receipt identity --------------------------------------------------------

    def test_request_and_receipt_identity_contradictions_refuse(self):
        with self.subTest("rehashed receipt with altered saved text"):
            self.reset()
            self.write_transcript([self.human()])
            self.build()
            receipt = json.loads(json.dumps(self.receipt))
            receipt["messages"][0]["text"] = "Different caller bytes."
            self.rewrite_receipt(receipt)
            report = self.audit_in_process()
            self.assertEqual(report["reasons"], ["receipt_integrity"])
        with self.subTest("invented role"):
            self.reset()
            self.write_transcript([self.human()])
            self.build()
            self.request["role"] = "supervisor"
            self.write_state()
            self.assertEqual(self.audit_in_process()["reasons"], ["request_integrity"])
        with self.subTest("missing retained provider turn identity"):
            self.reset()
            self.write_transcript([self.human()])
            self.build()
            self.request.pop("provider_turn_id")
            self.write_state()
            self.assertEqual(self.audit_in_process()["reasons"], ["request_integrity"])
        with self.subTest("conflicting receipt settings"):
            self.reset()
            self.write_transcript([self.human()])
            self.build()
            receipt = json.loads(json.dumps(self.receipt))
            receipt["settings"]["model"] = "other-model"
            self.rewrite_receipt(receipt)
            report = self.audit_in_process()
            self.assertEqual(report["reasons"], ["receipt_integrity"])
        with self.subTest("contradictory request reroute"):
            self.reset()
            self.write_transcript([self.human()])
            self.build()
            self.request["model_reroute"] = "rerouted"
            self.write_state()
            self.assertEqual(self.audit_in_process()["reasons"], ["request_integrity"])
        with self.subTest("contradictory receipt reroute"):
            self.reset()
            self.write_transcript([self.human()])
            self.build()
            receipt = json.loads(json.dumps(self.receipt))
            receipt["modelReroute"] = {"toModel": "other-model"}
            self.rewrite_receipt(receipt)
            report = self.audit_in_process()
            self.assertEqual(report["reasons"], ["receipt_integrity"])
        with self.subTest("non-Claude harness never reads the Claude owner"):
            self.reset()
            self.build(harness="codex")
            with mock.patch.object(ao_evidence_audit, "_read_owner",
                                   side_effect=AssertionError("owner reader must not run")):
                report = self.audit_in_process()
            self.assertEqual(report["reasons"], ["unsupported_harness"])

    # ---- D58-I2: preparation profile and epochs --------------------------------------------------

    def test_preparation_profile_is_the_recorded_delegate_and_routing_root(self):
        self.write_transcript([self.human()])
        self.build(preparation_overrides={"provider": "deepseek"})
        self.assertEqual(self.audit_in_process()["coverage"], "complete")
        for name, override in (("invented top-level config_root only",
                                {"config_root": str(self.config_root), "routing": None}),
                               ("missing routing root", {"routing": {"version": 2}}),
                               ("unsupported routing version", {"routing": {"version": 9,
                                                                             "claude_config_dir": str(self.config_root)}})):
            with self.subTest(name):
                self.reset()
                self.write_transcript([self.human()])
                self.build(preparation_overrides=override)
                report = self.audit_in_process()
                self.assertEqual(report["coverage"], "unavailable")
                self.assertEqual(report["reasons"], ["preparation_unbound"])
        with self.subTest("recorded delegate mismatch"):
            self.reset()
            self.write_transcript([self.human()])
            self.build(preparation_overrides={"delegate_sha256": canon({"delegate": "other"})})
            self.assertEqual(self.audit_in_process()["reasons"], ["preparation_unbound"])
        with self.subTest("historical v1 routing preparation"):
            self.reset()
            self.write_transcript([self.human()])
            self.build(routing_version=1)
            self.assertEqual(self.audit_in_process()["coverage"], "complete")

    def test_epoch_path_without_retained_transition_is_not_authority(self):
        self.write_transcript([self.human()])
        delegate = {"provider": "deepseek", "inventory": {"files": {}}, "files": {}, "policy_path": "p", "epoch": 2}
        self.build(delegate=delegate, preparation_relative="provider-transition/epochs/2/preparation.json",
                   preparation_overrides={"epoch": 2, "original_preparation": "preparation.json",
                                          "original_preparation_sha256": canon({"original": 1})})
        report = self.audit_in_process()
        self.assertEqual(report["coverage"], "unavailable")
        self.assertIn("preparation_unbound", report["reasons"])

    # ---- D58-I3: canonical native UUID discovery under projects ----------------------------------

    def test_native_uuid_discovery_under_projects(self):
        self.write_transcript([self.human()])
        self.build()
        report = self.audit_in_process()
        self.assertEqual(report["coverage"], "complete")
        self.assertEqual(report["sources"][0]["actor_sha256"],
                         canon({"owner_sha256": report["owner_sha256"], "kind": "parent", "agent_id": None}))
        moved = self.config_root / "projects" / (NATIVE_UUID + ".jsonl")
        moved.write_bytes(self.transcript.read_bytes())
        self.transcript.unlink()
        report = self.audit_in_process()
        self.assertEqual(report["coverage"], "unavailable")
        self.assertEqual(report["reasons"], ["source_missing"])

    def test_two_projects_with_the_same_native_file_conflict(self):
        self.write_transcript([self.human()])
        self.build()
        second = self.config_root / "projects" / "second-project"
        second.mkdir()
        (second / (NATIVE_UUID + ".jsonl")).write_bytes(self.transcript.read_bytes())
        report = self.audit_in_process()
        self.assertEqual(report["coverage"], "unavailable")
        self.assertEqual(report["reasons"], ["identity_conflict"])

    # ---- D58-I8: malformed evidence uses closed reasons ------------------------------------------

    def test_malformed_evidence_never_raises(self):
        with self.subTest("truthy non-object observed_turn"):
            self.reset()
            self.write_transcript([self.human()])
            self.build()
            self.request["observed_turn"] = "yes"
            self.write_state()
            self.assertEqual(self.audit_in_process()["reasons"], ["request_integrity"])
        with self.subTest("whitespace-only controller generation"):
            self.reset()
            self.write_transcript([self.human()])
            self.build()
            with sqlite3.connect(self.database) as db:
                db.execute("UPDATE sessions SET controller_generation='   '")
            report = self.audit_in_process()
            self.assertEqual(report["coverage"], "unavailable")
            self.assertEqual(report["reasons"], ["owner_unavailable"])
        with self.subTest("AO session identifier is not required to be a UUID"):
            self.reset()
            self.write_transcript([self.human()])
            self.build()
            self.assertFalse(AO_SESSION_ID == NATIVE_UUID)
            report = self.audit_in_process()
            self.assertEqual(report["coverage"], "complete")
        with self.subTest("saved text hash mismatch never raises"):
            self.reset()
            self.write_transcript([self.human()])
            self.build()
            self.request["text_sha256"] = sha("other text")
            self.write_state()
            self.assertEqual(self.audit_in_process()["reasons"], ["request_integrity"])


if __name__ == "__main__":
    unittest.main()
