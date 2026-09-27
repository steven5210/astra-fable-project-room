"""Offline root Read admission tests; every transcript, file and event is synthetic.

The tests exercise the deny-only guard directly and as a copied-alone hook
script over fake native records. They use no model, provider, credential,
network, live native transcript or private room state, and they claim nothing
about live native hook compatibility.
"""

import errno
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import ao_routing_guard as guard


class ReadAdmissionTests(unittest.TestCase):
    """One synthetic prepared root engineer session with fake native records."""

    SESSION = "00000000-0000-4000-8000-000000000001"

    def setUp(self):
        foreground = mock.patch.dict(os.environ, {"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"})
        foreground.start()
        self.addCleanup(foreground.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        self.session = self.SESSION
        self.project = self.root / "claude" / "projects" / "synthetic-project"
        self.project.mkdir(parents=True)
        self.transcript = self.project / (self.session + ".jsonl")
        self.rows = []
        self.write_transcript()
        for directory in (self.worktree, self.root / "claude", self.root / "claude" / "projects",
                          self.project):
            directory.chmod(0o755)

    # -- synthetic fixtures -------------------------------------------------
    def assistant_row(self, message_id, items, identity="assistant-record", **fields):
        row = {"type": "assistant", "uuid": identity, "sessionId": self.session,
               "cwd": str(self.worktree), "isSidechain": False,
               "timestamp": "2026-01-01T00:00:00.000Z",
               "message": {"role": "assistant", "id": message_id, "model": "claude-fable-test",
                           "content": items}}
        row.update(fields)
        return row

    def read_item(self, tool_use_id, params):
        return {"type": "tool_use", "id": tool_use_id, "name": "Read", "input": params}

    def caller_row(self, identity="caller-one", text="Continue the authorized work."):
        return {"type": "user", "uuid": identity, "sessionId": self.session,
                "cwd": str(self.worktree), "isSidechain": False,
                "timestamp": "2026-01-01T00:00:00.000Z", "origin": {"kind": "human"},
                "message": {"role": "user", "content": [{"type": "text", "text": text}]}}

    def queue_row(self, operation="enqueue"):
        # Observed native auxiliary record shape: no uuid and no message object.
        return {"type": "queue-operation", "operation": operation,
                "timestamp": "2026-01-01T00:00:00.000Z", "sessionId": self.session}

    def write_transcript(self, rows=None):
        if rows is not None:
            self.rows = rows
        self.transcript.write_text("".join(json.dumps(row) + "\n" for row in self.rows))
        self.transcript.chmod(0o600)

    def source(self, name, size):
        """A synthetic text file of exactly ``size`` bytes in 40-byte lines."""
        path = self.worktree / name
        whole, rest = divmod(size, 40)
        path.write_bytes((b"x" * 39 + b"\n") * whole + b"x" * rest)
        return path

    def message(self, message_id, tool_uses, identity="assistant-record"):
        return self.assistant_row(message_id,
                                  [self.read_item(tool_id, params) for tool_id, params in tool_uses],
                                  identity=identity)

    def event(self, tool_use_id, params):
        return {"tool_name": "Read", "tool_input": params, "session_id": self.session,
                "transcript_path": str(self.transcript), "cwd": str(self.worktree),
                "tool_use_id": tool_use_id}

    def decide(self, tool_use_id, params):
        return guard.decide(self.event(tool_use_id, params))

    def state_directory(self):
        return self.project / guard.READ_STATE_DIR / self.session

    def record_path(self, message_id):
        return self.state_directory() / (guard._message_digest(message_id) + ".json")

    def record(self, message_id):
        return json.loads(self.record_path(message_id).read_text())

    def run_guard_processes(self, events):
        """Run one guard process per event, all alive together, and reap every child.

        Each child receives its event through a file-backed stdin, so it reaches EOF
        immediately and all children stay alive together while `communicate` is never
        asked to flush a pipe this harness closed by hand. The `finally` block reaps
        and closes every child even on assertion, timeout or interrupt.
        """
        script = self.root / ("standalone-guard-%d.py" % len(events))
        shutil.copyfile(guard.__file__, str(script))
        processes = []
        try:
            for index, event in enumerate(events):
                payload = self.root / ("standalone-event-%d.json" % index)
                payload.write_text(json.dumps(event))
                with payload.open("rb") as stream:
                    processes.append(subprocess.Popen([sys.executable, "-I", str(script)],
                                                      stdin=stream, stdout=subprocess.PIPE,
                                                      stderr=subprocess.PIPE, text=True,
                                                      cwd=str(self.worktree)))
            results = []
            for process in processes:
                stdout, stderr = process.communicate(timeout=120)
                results.append((process.returncode, stdout, stderr))
            return results
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.wait()
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        try:
                            stream.close()
                        except OSError:
                            pass

    # -- native message grouping and budgets --------------------------------
    def test_observed_scale_message_is_grouped_by_native_message_and_bounded(self):
        # Observed evidence: thirteen separately recorded Read calls shared one native
        # assistant message identity; their returned UTF-8 text including native line
        # numbers summed to 594020 bytes over roughly 562 KB of selected source. These
        # synthetic files mirror that returned scale; the budgets below are product
        # admission limits and are not that measurement.
        sizes = [50262, 25396, 42002, 45581, 44875, 53073, 37714, 38722, 40274, 49319,
                 50148, 63249, 53405]
        tool_uses = [("toolu_scale_%02d" % index, {"file_path": str(self.source("scale-%02d.txt" % index, size))})
                     for index, size in enumerate(sizes)]
        rows = [self.caller_row(), self.queue_row()]
        for start in range(0, len(tool_uses), 4):
            rows.append(self.assistant_row("msg-observed-scale",
                                           [self.read_item(tool_id, params)
                                            for tool_id, params in tool_uses[start:start + 4]],
                                           identity="assistant-stream-%d" % start))
        rows.append(self.queue_row("dequeue"))
        self.write_transcript(rows)
        decisions = [self.decide(tool_id, params) for tool_id, params in tool_uses]
        admitted = [index for index, reason in enumerate(decisions) if reason is None]
        self.assertEqual(admitted, [1])  # only the 25396-byte file is inside the per-read budget
        for index, reason in enumerate(decisions):
            if index != 1:
                self.assertIn("Read admission", reason)
                self.assertIn("per-read admission budget", reason)
                self.assertNotIn("/clear", reason)
        record = self.record("msg-observed-scale")
        self.assertEqual(record["total_raw_bytes"], 25396)
        self.assertEqual(len(record["entries"]), 1)
        self.assertLessEqual(record["total_raw_bytes"], guard.READ_MAX_MESSAGE_BYTES)
        # The exact same tool call remains one idempotent reservation.
        self.assertIsNone(self.decide(*tool_uses[1]))
        self.assertEqual(self.record("msg-observed-scale"), record)

    def test_message_budget_spans_individually_admissible_bounded_reads(self):
        tool_uses = [("toolu_aggregate_%02d" % index,
                      {"file_path": str(self.source("aggregate-%02d.txt" % index, 6000))})
                     for index in range(13)]
        self.write_transcript([self.caller_row(), self.message("msg-aggregate", tool_uses)])
        decisions = [self.decide(tool_id, params) for tool_id, params in tool_uses]
        admitted = [index for index, reason in enumerate(decisions) if reason is None]
        self.assertEqual(admitted, list(range(10)))
        record = self.record("msg-aggregate")
        self.assertEqual(record["total_raw_bytes"], 60000)
        self.assertLessEqual(record["total_raw_bytes"], guard.READ_MAX_MESSAGE_BYTES)
        self.assertEqual(record["total_raw_bytes"],
                         sum(entry["raw_bytes"] for entry in record["entries"].values()))
        for reason in decisions[10:]:
            self.assertIn("per-message admission budget", reason)
            self.assertIn("later native assistant message", reason)

    def test_two_native_messages_in_one_human_turn_have_separate_budgets(self):
        first = [("toolu_turn_one_%02d" % index,
                  {"file_path": str(self.source("turn-one-%02d.txt" % index, 6000))})
                 for index in range(10)]
        second = [("toolu_turn_two_%02d" % index,
                   {"file_path": str(self.source("turn-two-%02d.txt" % index, 6000))})
                  for index in range(10)]
        self.write_transcript([self.caller_row(), self.message("msg-turn-one", first)])
        for tool_id, params in first:
            self.assertIsNone(self.decide(tool_id, params))
        self.rows.append(self.assistant_row("msg-turn-two",
                                            [self.read_item(tool_id, params)
                                             for tool_id, params in second],
                                            identity="assistant-turn-two"))
        self.write_transcript()
        for tool_id, params in second:
            self.assertIsNone(self.decide(tool_id, params))
        self.assertEqual(self.record("msg-turn-one")["total_raw_bytes"], 60000)
        self.assertEqual(self.record("msg-turn-two")["total_raw_bytes"], 60000)
        # Once a later native message exists, the older call is stale even though it
        # was already reserved.
        self.assertIn("stale", self.decide(*first[0]))

    # -- identity, conflicts, staleness -------------------------------------
    def test_missing_or_unflushed_native_identity_is_never_guessed(self):
        params = {"file_path": str(self.source("absent.txt", 100))}
        self.write_transcript([self.caller_row()])
        reason = self.decide("toolu_absent", params)
        self.assertIn("could not be positively correlated", reason)
        self.assertIn("flushed", reason)
        self.assertFalse(self.state_directory().exists())
        with self.transcript.open("ab") as stream:
            stream.write(b'{"type":"assistant","message":{"id":"msg-partial')
        reason = self.decide("toolu_absent", params)
        self.assertIn("still incomplete", reason)
        self.assertFalse(self.state_directory().exists())
        self.rows.append(self.assistant_row("msg-flushed", [self.read_item("toolu_absent", params)]))
        self.write_transcript()
        self.assertIsNone(self.decide("toolu_absent", params))

    def test_conflicting_native_identities_are_refused(self):
        params = {"file_path": str(self.source("conflict.txt", 100))}
        cases = []
        cases.append(("conflicting duplicate",
                      [self.caller_row(),
                       self.assistant_row("msg-conflict", [self.read_item("toolu_conflict", params)],
                                          identity="assistant-shared"),
                       self.assistant_row("msg-conflict", [self.read_item("toolu_other", params)],
                                          identity="assistant-shared")], "toolu_conflict"))
        cases.append(("different Read arguments",
                      [self.caller_row(),
                       self.assistant_row("msg-conflict", [self.read_item("toolu_conflict", params)]),
                       self.assistant_row("msg-conflict", [self.read_item("toolu_conflict", {"file_path": "/other"})],
                                          identity="assistant-other-input")], "toolu_conflict"))
        cases.append(("more than one native assistant message",
                      [self.caller_row(),
                       self.assistant_row("msg-one-conflict", [self.read_item("toolu_conflict", params)]),
                       self.assistant_row("msg-two-conflict", [self.read_item("toolu_conflict", params)],
                                          identity="assistant-second-message")], "toolu_conflict"))
        cases.append(("different tool",
                      [self.caller_row(),
                       self.assistant_row("msg-conflict", [{"type": "tool_use", "id": "toolu_conflict",
                                                             "name": "Bash", "input": params}])],
                      "toolu_conflict"))
        for expected, rows, tool_use_id in cases:
            with self.subTest(expected=expected):
                self.write_transcript(rows)
                reason = self.decide(tool_use_id, params)
                self.assertIn("Read admission", reason)
                self.assertIn(expected, reason)
                self.assertFalse(self.state_directory().exists())

    def test_later_native_message_or_human_turn_makes_an_old_read_stale(self):
        params = {"file_path": str(self.source("stale.txt", 1000))}
        self.write_transcript([self.caller_row(),
                               self.message("msg-old", [("toolu_old", params)]),
                               self.assistant_row("msg-newer", [], identity="assistant-newer")])
        self.assertIn("stale", self.decide("toolu_old", params))
        self.write_transcript([self.caller_row(),
                               self.message("msg-old", [("toolu_old", params)]),
                               self.caller_row("caller-two", "A newer human instruction.")])
        self.assertIn("stale", self.decide("toolu_old", params))
        self.write_transcript([self.caller_row(),
                               self.message("msg-current", [("toolu_old", params)])])
        self.assertIsNone(self.decide("toolu_old", params))

    # -- measurement boundaries and remedies --------------------------------
    def test_line_ranges_unicode_crlf_and_eof_are_measured_conservatively(self):
        lines_file = self.worktree / "ranges.txt"
        lines_file.write_text("".join("line %03d\n" % index for index in range(100)))
        unicode_file = self.worktree / "unicode.txt"
        unicode_file.write_text("\u00e9" * 100 + "\n", encoding="utf-8")
        bytes_file = self.worktree / "bytes-not-characters.txt"
        bytes_file.write_text("\u00e9" * 20000, encoding="utf-8")
        crlf_file = self.worktree / "crlf.txt"
        crlf_file.write_bytes(b"ab\r\ncd\r\n")
        tail_file = self.worktree / "no-newline.txt"
        tail_file.write_bytes(b"abc")
        empty_file = self.worktree / "empty.txt"
        empty_file.write_bytes(b"")
        relative_file = self.worktree / "relative-range.txt"
        relative_file.write_bytes(b"ab\ncd\n")
        calls = [("toolu_range_one", {"file_path": str(lines_file), "offset": 1, "limit": 10}),
                 ("toolu_range_two", {"file_path": str(lines_file), "offset": 11, "limit": 10}),
                 ("toolu_unicode", {"file_path": str(unicode_file)}),
                 ("toolu_crlf", {"file_path": str(crlf_file), "offset": 1, "limit": 1}),
                 ("toolu_tail", {"file_path": str(tail_file)}),
                 ("toolu_relative", {"file_path": "relative-range.txt", "offset": 1, "limit": 1}),
                 ("toolu_empty", {"file_path": str(empty_file)}),
                 ("toolu_beyond", {"file_path": str(lines_file), "offset": 500, "limit": 10}),
                 ("toolu_big_unicode", {"file_path": str(bytes_file)})]
        self.write_transcript([self.caller_row(), self.message("msg-mixed", calls)])
        for tool_id, params in calls[:-1]:
            with self.subTest(tool_use_id=tool_id):
                self.assertIsNone(self.decide(tool_id, params))
        self.assertIn("per-read admission budget", self.decide(*calls[-1]))
        record = self.record("msg-mixed")
        self.assertEqual(record["total_raw_bytes"], 391)
        self.assertEqual(record["total_lines"], 24)
        self.assertEqual(record["entries"]["toolu_range_one"]["raw_bytes"], 90)
        self.assertEqual(record["entries"]["toolu_range_two"]["raw_bytes"], 90)
        self.assertEqual(record["entries"]["toolu_unicode"]["raw_bytes"], 201)
        self.assertEqual(record["entries"]["toolu_crlf"]["raw_bytes"], 4)
        self.assertEqual(record["entries"]["toolu_tail"]["raw_bytes"], 3)
        self.assertEqual(record["entries"]["toolu_relative"]["raw_bytes"], 3)
        self.assertEqual(record["entries"]["toolu_empty"]["raw_bytes"], 0)
        self.assertEqual(record["entries"]["toolu_beyond"]["raw_bytes"], 0)

    def test_huge_line_and_unreachable_prefix_denials_are_truthful(self):
        huge = self.worktree / "huge-line.txt"
        huge.write_bytes(b"z" * (guard.READ_MAX_SELECTED_BYTES + 5000))
        single = {"file_path": str(huge), "offset": 1, "limit": 1}
        self.write_transcript([self.caller_row(), self.message("msg-huge", [("toolu_huge", single)])])
        reason = self.decide("toolu_huge", single)
        self.assertIn("single line", reason)
        self.assertIn("cannot make one line fit", reason)
        self.assertNotIn("/clear", reason)
        self.assertNotIn("earlier offset", reason)
        prefix = self.worktree / "giant-prefix.txt"
        prefix.write_bytes((b"p" * 19999 + b"\n") * 200)
        beyond = {"file_path": str(prefix), "offset": 150, "limit": 1}
        near = {"file_path": str(prefix), "offset": 1, "limit": 1}
        two_lines = self.worktree / "two-big-lines.txt"
        two_lines.write_bytes(b"a" * 19999 + b"\n" + b"b" * 19999 + b"\n")
        both = {"file_path": str(two_lines), "offset": 1, "limit": 2}
        first_only = {"file_path": str(two_lines), "offset": 1, "limit": 1}
        self.write_transcript([self.caller_row(), self.message("msg-prefix", [
            ("toolu_prefix", beyond), ("toolu_prefix_near", near),
            ("toolu_two_lines", both), ("toolu_first_only", first_only)])])
        reason = self.decide("toolu_prefix", beyond)
        self.assertIn("begins after more than the bounded", reason)
        self.assertIn("cannot verify", reason)
        self.assertIsNone(self.decide("toolu_prefix_near", near))
        reason = self.decide("toolu_two_lines", both)
        self.assertIn("fewer lines", reason)
        self.assertIsNone(self.decide("toolu_first_only", first_only))

    def test_malformed_arguments_and_unsupported_read_forms_are_refused(self):
        path = str(self.source("arguments.txt", 100))
        cases = [{"file_path": path, "pages": [1, 2]},
                 {"file_path": path, "mode": "pdf"},
                 {"file_path": path, "offset": True},
                 {"file_path": path, "offset": None},
                 {"file_path": path, "offset": 0},
                 {"file_path": path, "offset": guard.READ_MAX_OFFSET + 1},
                 {"file_path": path, "limit": 1.5},
                 {"file_path": path, "limit": None},
                 {"file_path": path, "limit": 0},
                 {"file_path": path, "limit": guard.READ_MAX_LIMIT + 1},
                 {"file_path": path, "offset": 1, "limit": 1, "extra": 1},
                 {"offset": 1},
                 {"file_path": ""},
                 {"file_path": 7}]
        for params in cases:
            with self.subTest(params=params), mock.patch.object(
                    guard.os, "pread", side_effect=AssertionError("unexpected read")):
                reason = self.decide("toolu_arguments", params)
                self.assertIn("Read admission", reason)
        self.assertFalse(self.state_directory().exists())

    def test_special_files_and_symlinked_paths_are_refused_without_reading(self):
        original = os.pread

        def guarded(fd, size, offset):
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise AssertionError("a non-regular target was read")
            return original(fd, size, offset)

        real = self.source("real.txt", 100)
        fifo = self.worktree / "pipe"
        os.mkfifo(fifo, 0o600)
        folder = self.worktree / "folder"
        folder.mkdir()
        link = self.worktree / "link.txt"
        link.symlink_to(real)
        dangling = self.worktree / "dangling.txt"
        dangling.symlink_to(self.worktree / "missing.txt")
        linked_parent = self.worktree / "linked-parent"
        linked_parent.symlink_to(self.worktree, target_is_directory=True)
        targets = [(fifo, "regular file"), (folder, "regular file"), (link, None), (dangling, None),
                   (linked_parent / "real.txt", None), (self.worktree / "missing-file.txt", None)]
        if Path("/dev/null").exists():
            targets.append((Path("/dev/null"), "regular file"))
        for index, (target, expected) in enumerate(targets):
            with self.subTest(target=target.name):
                params = {"file_path": str(target)}
                tool_use_id = "toolu_special_%02d" % index
                self.write_transcript([self.caller_row(),
                                       self.message("msg-special-%02d" % index,
                                                    [(tool_use_id, params)])])
                with mock.patch.object(guard.os, "pread", side_effect=guarded):
                    reason = self.decide(tool_use_id, params)
                self.assertIn("Read admission", reason)
                if expected is not None:
                    self.assertIn(expected, reason)

    # -- durable state ------------------------------------------------------
    def test_persisted_state_is_validated_and_never_repaired_or_deleted(self):
        params = {"file_path": str(self.source("state.txt", 1000))}
        self.write_transcript([self.caller_row(), self.message("msg-state", [("toolu_state", params)])])
        self.assertIsNone(self.decide("toolu_state", params))
        record_path = self.record_path("msg-state")
        good = record_path.read_bytes()
        record = json.loads(good)
        record["total_raw_bytes"] = 0
        tampered = json.dumps(record).encode("utf-8")
        record_path.write_bytes(tampered)
        self.assertIn("totals do not match", self.decide("toolu_state", params))
        self.assertEqual(record_path.read_bytes(), tampered)
        record_path.write_bytes(good[:20])
        self.assertIn("Read admission", self.decide("toolu_state", params))
        self.assertEqual(record_path.read_bytes(), good[:20])
        record = json.loads(good)
        record["entries"]["toolu_state"]["lines"] = True
        record_path.write_text(json.dumps(record))
        self.assertIn("Read admission", self.decide("toolu_state", params))
        self.assertTrue(record_path.exists())
        # The guard itself never deletes persisted evidence.
        source = Path(guard.__file__).read_text()
        for marker in ("os.unlink", "os.remove", "os.rmdir", "shutil.rmtree"):
            self.assertNotIn(marker, source)

    def test_state_capacity_and_foreign_entries_fail_closed(self):
        # The intended capacity check needs an append-only, current-message history:
        # a rewound or stale transcript refuses earlier, for a different reason, and
        # that staleness/rewind behaviour is covered separately.
        first = {"file_path": str(self.source("capacity-one.txt", 1000))}
        self.write_transcript([self.caller_row(), self.message("msg-capacity-one", [("toolu_capacity_one", first)])])
        self.assertIsNone(self.decide("toolu_capacity_one", first))
        second = {"file_path": str(self.source("capacity-two.txt", 1000))}
        self.rows.append(self.assistant_row("msg-capacity-two", [self.read_item("toolu_capacity_two", second)],
                                            identity="assistant-capacity-two"))
        self.write_transcript()
        with mock.patch.object(guard, "READ_STATE_MAX_RECORDS", 1):
            reason = self.decide("toolu_capacity_two", second)
        self.assertIn("documented", reason)
        self.assertIn("capacity", reason)
        self.assertTrue(self.record_path("msg-capacity-one").exists())
        self.rows.append(self.assistant_row("msg-capacity-three", [self.read_item("toolu_capacity_three", second)],
                                            identity="assistant-capacity-three"))
        self.write_transcript()
        foreign = self.state_directory() / "foreign.bin"
        foreign.write_bytes(b"not mine")
        reason = self.decide("toolu_capacity_three", second)
        self.assertIn("does not recognize", reason)
        self.assertEqual(foreign.read_bytes(), b"not mine")

    def test_unsafe_state_directories_and_lock_files_are_refused(self):
        params = {"file_path": str(self.source("unsafe-state.txt", 100))}
        self.write_transcript([self.caller_row(), self.message("msg-unsafe-state", [("toolu_unsafe_state", params)])])
        state_root = self.project / guard.READ_STATE_DIR
        outside = self.root / "outside-state"
        outside.mkdir()
        state_root.symlink_to(outside, target_is_directory=True)
        self.assertIn("Read admission", self.decide("toolu_unsafe_state", params))
        self.assertEqual(list(outside.iterdir()), [])
        state_root.unlink()
        state_root.mkdir(mode=0o700)
        state_root.chmod(0o777)
        self.assertIn("Read admission", self.decide("toolu_unsafe_state", params))
        state_root.chmod(0o700)
        state_root.rmdir()
        session_directory = self.state_directory()
        session_directory.mkdir(parents=True)
        session_directory.chmod(0o700)
        os.mkfifo(session_directory / guard.READ_LOCK_NAME, 0o600)
        self.assertIn("lock file", self.decide("toolu_unsafe_state", params))
        (session_directory / guard.READ_LOCK_NAME).unlink()
        os.mkfifo(session_directory / (guard._message_digest("msg-unsafe-state") + ".json"), 0o600)
        self.assertIn("not an owned private regular file", self.decide("toolu_unsafe_state", params))

    def test_changed_target_cannot_reuse_an_old_small_reservation(self):
        params = {"file_path": str(self.source("changed-target.txt", 1000))}
        self.write_transcript([self.caller_row(), self.message("msg-changed", [("toolu_changed", params)])])
        self.assertIsNone(self.decide("toolu_changed", params))
        record = self.record("msg-changed")
        self.assertEqual(record["total_raw_bytes"], 1000)
        self.source("changed-target.txt", 5000)
        reason = self.decide("toolu_changed", params)
        self.assertIn("changed after this call", reason)
        self.assertEqual(self.record("msg-changed"), record)

    # -- concurrency, restart and copied-alone execution --------------------
    def test_concurrent_processes_share_one_durable_reservation_ledger(self):
        tool_uses = [("toolu_concurrent_%02d" % index,
                      {"file_path": str(self.source("concurrent-%02d.txt" % index, 10000))})
                     for index in range(12)]
        self.write_transcript([self.caller_row(), self.message("msg-concurrent", tool_uses)])
        results = self.run_guard_processes([self.event(tool_id, params) for tool_id, params in tool_uses])
        admitted = sum(1 for returncode, stdout, _ in results if (returncode, stdout) == (0, ""))
        self.assertEqual(admitted, 6)
        for returncode, stdout, _ in results:
            self.assertEqual(returncode, 0)
            if stdout:
                decision = json.loads(stdout)["hookSpecificOutput"]
                self.assertEqual(decision["permissionDecision"], "deny")
        record = self.record("msg-concurrent")
        self.assertEqual(record["total_raw_bytes"], 60000)
        self.assertEqual(len(record["entries"]), 6)
        self.assertEqual(record["total_raw_bytes"],
                         sum(entry["raw_bytes"] for entry in record["entries"].values()))

    def test_concurrent_identical_calls_charge_one_reservation(self):
        params = {"file_path": str(self.source("identical.txt", 10000))}
        self.write_transcript([self.caller_row(), self.message("msg-identical", [("toolu_identical", params)])])
        results = self.run_guard_processes([self.event("toolu_identical", params)] * 8)
        for returncode, stdout, _ in results:
            self.assertEqual((returncode, stdout), (0, ""))
        record = self.record("msg-identical")
        self.assertEqual(len(record["entries"]), 1)
        self.assertEqual(record["total_raw_bytes"], 10000)

    def test_reservations_survive_restarts_and_scratch_evidence_is_not_read(self):
        files = [self.source("restart-%d.txt" % index, 12000) for index in range(3)]
        tool_uses = [("toolu_restart_%d" % index, {"file_path": str(files[index])})
                     for index in range(3)]
        self.write_transcript([self.caller_row(), self.message("msg-restart", tool_uses)])
        results = self.run_guard_processes([self.event(tool_id, params) for tool_id, params in tool_uses])
        for returncode, stdout, _ in results:
            self.assertEqual((returncode, stdout), (0, ""))
        self.assertEqual(self.record("msg-restart")["total_raw_bytes"], 36000)
        scratch = self.state_directory() / (guard.READ_SCRATCH_PREFIX + "interrupted-evidence")
        scratch.write_bytes(b"interrupted reservation evidence")
        fourth = self.source("restart-four.txt", 12000)
        self.rows.append(self.assistant_row("msg-restart",
                                            [self.read_item("toolu_restart_four", {"file_path": str(fourth)})],
                                            identity="assistant-restart-four"))
        self.write_transcript()
        self.assertIsNone(self.decide("toolu_restart_four", {"file_path": str(fourth)}))
        self.assertEqual(self.record("msg-restart")["total_raw_bytes"], 48000)
        self.assertEqual(scratch.read_bytes(), b"interrupted reservation evidence")

    def test_copied_alone_guard_only_denies_and_never_echoes_content(self):
        script = self.root / "standalone-read-guard.py"
        shutil.copyfile(guard.__file__, str(script))
        small = {"file_path": str(self.source("standalone-small.txt", 100))}
        big = self.worktree / "standalone-big.txt"
        big.write_bytes(b"CANARY-" * 6000)
        big_params = {"file_path": str(big)}
        self.write_transcript([self.caller_row(),
                               self.message("msg-standalone", [("toolu_standalone_small", small),
                                                                ("toolu_standalone_big", big_params)])])
        run = lambda event: subprocess.run([sys.executable, "-I", str(script)], input=json.dumps(event),
                                           capture_output=True, text=True, cwd=str(self.worktree),
                                           timeout=120)
        admitted = run(self.event("toolu_standalone_small", small))
        self.assertEqual((admitted.returncode, admitted.stdout, admitted.stderr), (0, "", ""))
        denied = run(self.event("toolu_standalone_big", big_params))
        self.assertEqual(denied.returncode, 0)
        decision = json.loads(denied.stdout)["hookSpecificOutput"]
        self.assertEqual(decision["hookEventName"], "PreToolUse")
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertNotIn("CANARY", denied.stdout + denied.stderr)
        self.assertNotIn(str(self.worktree), denied.stdout + denied.stderr)

    # -- corrected admission regressions ------------------------------------
    def test_dot_and_dotdot_components_are_refused_before_any_measurement(self):
        outside = self.root / "outside-dotdot"
        outside.mkdir()
        (outside / "dotdot-target.txt").write_bytes(b"o" * 100_000)
        self.source("dotdot-target.txt", 100)
        (self.worktree / "dotdot-link").symlink_to(outside, target_is_directory=True)
        paths = ["dotdot-link/../dotdot-target.txt", "./dotdot-target.txt", "dotdot-link/./target.txt",
                 str(self.worktree / "dotdot-link" / ".." / "dotdot-target.txt"),
                 str(self.worktree) + "/./dotdot-target.txt",
                 str(self.worktree) + "/../worktree/dotdot-target.txt"]
        for index, file_path in enumerate(paths):
            with self.subTest(file_path=file_path):
                tool_use_id = "toolu_dotdot_%02d" % index
                params = {"file_path": file_path}
                self.write_transcript([self.caller_row(),
                                       self.message("msg-dotdot-%02d" % index, [(tool_use_id, params)])])
                with mock.patch.object(guard, "_measure_read",
                                       side_effect=AssertionError("unsafe path was measured")):
                    reason = self.decide(tool_use_id, params)
                self.assertIn("unsafe path components", reason)
        self.assertFalse((self.project / guard.READ_STATE_DIR).exists())

    def test_incomplete_later_native_record_refuses_even_after_a_match(self):
        params = {"file_path": str(self.source("partial-tail.txt", 100))}
        self.write_transcript([self.caller_row(),
                               self.message("msg-partial-tail", [("toolu_partial_tail", params)])])
        with self.transcript.open("ab") as stream:
            stream.write(b'{"type":"assistant","message":{"id":"msg-unfinished"')
        reason = self.decide("toolu_partial_tail", params)
        self.assertIn("still incomplete", reason)
        self.assertFalse(self.state_directory().exists())

    def test_correlation_is_revalidated_under_the_reservation_transaction(self):
        params = {"file_path": str(self.source("transaction-race.txt", 1000))}
        original = guard._measure_read

        def later_caller(path, offset, limit):
            result = original(path, offset, limit)
            self.rows.append(self.caller_row("caller-transaction", "A later human instruction."))
            self.write_transcript()
            return result

        self.write_transcript([self.caller_row(),
                               self.message("msg-transaction", [("toolu_transaction", params)])])
        with mock.patch.object(guard, "_measure_read", side_effect=later_caller):
            reason = self.decide("toolu_transaction", params)
        self.assertIn("stale", reason)

        def partial_suffix(path, offset, limit):
            result = original(path, offset, limit)
            with self.transcript.open("ab") as stream:
                stream.write(b'{"type":"user","uuid":"caller-transaction"')
            return result

        self.write_transcript([self.caller_row(),
                               self.message("msg-transaction-two", [("toolu_transaction_two", params)])])
        with mock.patch.object(guard, "_measure_read", side_effect=partial_suffix):
            reason = self.decide("toolu_transaction_two", params)
        self.assertIn("still incomplete", reason)
        self.assertFalse(self.record_path("msg-transaction").exists())
        self.assertFalse(self.record_path("msg-transaction-two").exists())

    def test_replaced_native_transcript_identity_is_refused(self):
        first = {"file_path": str(self.source("replacement-one.txt", 1000))}
        self.write_transcript([self.caller_row(),
                               self.message("msg-replacement-one", [("toolu_replacement_one", first)])])
        self.assertIsNone(self.decide("toolu_replacement_one", first))
        second = {"file_path": str(self.source("replacement-two.txt", 1000))}
        replacement = self.root / "replacement-transcript.jsonl"
        replacement.write_text("".join(json.dumps(row) + "\n" for row in (
            self.caller_row(),
            self.message("msg-replacement-two", [("toolu_replacement_two", second)]))))
        replacement.chmod(0o600)
        os.replace(replacement, self.transcript)
        reason = self.decide("toolu_replacement_two", second)
        self.assertIn("replaced", reason)
        self.assertFalse(self.record_path("msg-replacement-two").exists())

    def test_rewound_native_transcript_is_refused(self):
        first = {"file_path": str(self.source("rewind-one.txt", 1000))}
        trailing = self.queue_row("enqueue")
        trailing["pad"] = "x" * 4096
        self.write_transcript([self.caller_row(),
                               self.message("msg-rewind-one", [("toolu_rewind_reuse", first)]),
                               trailing])
        self.assertIsNone(self.decide("toolu_rewind_reuse", first))
        second = {"file_path": str(self.source("rewind-two.txt", 1000))}
        self.write_transcript([self.caller_row(),
                               self.message("msg-rewind-two", [("toolu_rewind_reuse", second)],
                                            identity="assistant-rewind-two")])
        reason = self.decide("toolu_rewind_reuse", second)
        self.assertIn("rewritten", reason)
        self.assertFalse(self.record_path("msg-rewind-two").exists())
        self.assertEqual(list(self.record("msg-rewind-one")["entries"]), ["toolu_rewind_reuse"])

    def test_reused_tool_identity_outside_the_transcript_window_is_refused(self):
        params = {"file_path": str(self.source("window-one.txt", 1000))}
        rows = [self.caller_row(), self.message("msg-window-one", [("toolu_window_reuse", params)])]
        self.write_transcript(rows)
        self.assertIsNone(self.decide("toolu_window_reuse", params))
        filler = []
        for index in range(9_000):
            row = self.queue_row("enqueue")
            row["pad"] = "%08d" % index + "y" * 240
            filler.append(row)
        reused = {"file_path": str(self.source("window-two.txt", 1000))}
        self.write_transcript(rows + filler + [
            self.message("msg-window-two", [("toolu_window_reuse", reused)],
                         identity="assistant-window-two")])
        reason = self.decide("toolu_window_reuse", reused)
        self.assertIn("bound to a different", reason)
        self.assertFalse(self.record_path("msg-window-two").exists())

    def test_binary_and_container_targets_are_refused_and_plain_utf8_still_works(self):
        png = self.worktree / "detected.txt"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
        invalid = self.worktree / "invalid-utf8.txt"
        invalid.write_bytes(b"text \xff\xfe more text\n")
        nul = self.worktree / "nul-bytes.txt"
        nul.write_bytes(b"readable start\x00binary tail\n")
        pdf = self.worktree / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        notebook = self.worktree / "analysis.ipynb"
        notebook.write_text(json.dumps({"cells": [], "nbformat": 4, "nbformat_minor": 5}))
        cases = [("PNG signature", png), ("invalid UTF-8", invalid), ("NUL byte", nul),
                 ("PDF container", pdf), ("notebook container", notebook)]
        text_call = ("toolu_container_text", {"file_path": str(self.source("plain-utf8.txt", 400))})
        tool_uses = [("toolu_container_%02d" % index, {"file_path": str(target)})
                     for index, (_label, target) in enumerate(cases)]
        self.write_transcript([self.caller_row(),
                               self.message("msg-containers", tool_uses + [text_call])])
        for (tool_use_id, params), (_label, target) in zip(tool_uses, cases):
            with self.subTest(target=target.name):
                self.assertIn("Read admission", self.decide(tool_use_id, params))
        self.assertFalse(self.record_path("msg-containers").exists())
        self.assertIsNone(self.decide(*text_call))
        self.assertEqual(self.record("msg-containers")["total_raw_bytes"], 400)

    def test_directory_inventory_is_validated_on_existing_message_additions(self):
        first = {"file_path": str(self.source("inventory-one.txt", 1000))}
        second = {"file_path": str(self.source("inventory-two.txt", 1000))}
        # Both calls are recorded in the initial native message: adding an entry to
        # an existing message ledger is the path under test, and it needs no
        # same-inode transcript rewrite (which continuity correctly refuses first).
        self.write_transcript([self.caller_row(),
                               self.message("msg-inventory", [("toolu_inventory_one", first),
                                                              ("toolu_inventory_two", second)])])
        self.assertIsNone(self.decide("toolu_inventory_one", first))
        scratch = self.state_directory() / (guard.READ_SCRATCH_PREFIX + "retained-evidence")
        scratch.write_bytes(b"interrupted reservation evidence")
        with mock.patch.object(guard, "READ_STATE_MAX_ENTRIES", 2):
            reason = self.decide("toolu_inventory_two", second)
        self.assertIn("documented bound", reason)
        self.assertEqual(scratch.read_bytes(), b"interrupted reservation evidence")
        self.assertEqual(len(self.record("msg-inventory")["entries"]), 1)

    def test_lost_message_ledger_with_retained_bindings_is_not_a_fresh_allowance(self):
        # Confirmed bypass reproduction: two 30000-byte reads fill 60000 of the
        # 65536-byte per-message budget, and deleting only the message ledger used to
        # admit a third 30000-byte read into a fresh ledger, so 90000 bytes were
        # admitted inside one native assistant message.
        first = {"file_path": str(self.source("ledger-loss-one.txt", 30000))}
        second = {"file_path": str(self.source("ledger-loss-two.txt", 30000))}
        third = {"file_path": str(self.source("ledger-loss-three.txt", 30000))}
        calls = [("toolu_ledger_loss_one", first), ("toolu_ledger_loss_two", second),
                 ("toolu_ledger_loss_three", third), ("toolu_ledger_loss_four", third)]
        self.write_transcript([self.caller_row(), self.message("msg-ledger-loss", calls)])
        self.assertIsNone(self.decide(*calls[0]))
        self.assertIsNone(self.decide(*calls[1]))
        self.assertEqual(sorted(self.record("msg-ledger-loss")["entries"]),
                         ["toolu_ledger_loss_one", "toolu_ledger_loss_two"])
        self.assertEqual(self.record("msg-ledger-loss")["total_raw_bytes"], 60000)
        self.assertIn("per-message admission budget", self.decide(*calls[2]))
        # The accepted bytes live in the ledger; losing that ledger is missing
        # recorded evidence, never a fresh per-message allowance.
        ledger = self.record_path("msg-ledger-loss")
        ledger.unlink()
        for tool_use_id, params in (calls[2], calls[3]):
            with self.subTest(tool_use_id=tool_use_id):
                reason = self.decide(tool_use_id, params)
                self.assertIn("ledger is missing", reason)
                self.assertIn("operator review", reason)
        self.assertFalse(ledger.exists())
        self.assertEqual(sorted(json.loads(
            (self.state_directory() / guard.READ_SESSION_NAME).read_text())["bindings"]),
            ["toolu_ledger_loss_one", "toolu_ledger_loss_two"])

    def test_missing_continuity_with_retained_ledgers_refuses_instead_of_resetting(self):
        first = {"file_path": str(self.source("continuity-loss-one.txt", 1000))}
        self.write_transcript([self.caller_row(),
                               self.message("msg-continuity-kept", [("toolu_continuity_kept", first)])])
        self.assertIsNone(self.decide("toolu_continuity_kept", first))
        ledger = self.record_path("msg-continuity-kept")
        kept = ledger.read_bytes()
        continuity = self.state_directory() / guard.READ_SESSION_NAME
        continuity.unlink()
        reused = {"file_path": str(self.source("continuity-loss-two.txt", 1000))}
        # Same-inode rewrite to a different native message that reuses the prior tool
        # identity: without the continuity record that durable identity protection
        # used to be reset and the read was admitted.
        self.write_transcript([self.caller_row(),
                               self.message("msg-continuity-reused",
                                            [("toolu_continuity_kept", reused)],
                                            identity="assistant-continuity-reused")])
        reason = self.decide("toolu_continuity_kept", reused)
        self.assertIn("continuity", reason)
        self.assertIn("operator review", reason)
        self.assertFalse(self.record_path("msg-continuity-reused").exists())
        self.assertEqual(ledger.read_bytes(), kept)
        self.assertFalse(continuity.exists())
        # A brand-new message is refused for the same reason: the lost continuity
        # record cannot be rebuilt or reset from evidence.
        fresh = {"file_path": str(self.source("continuity-loss-three.txt", 1000))}
        self.write_transcript([self.caller_row(),
                               self.message("msg-continuity-new", [("toolu_continuity_new", fresh)],
                                            identity="assistant-continuity-new")])
        self.assertIn("continuity", self.decide("toolu_continuity_new", fresh))
        self.assertFalse(self.record_path("msg-continuity-new").exists())

    def test_interrupted_first_reservation_is_stopped_not_completed_on_retry(self):
        params = {"file_path": str(self.source("interrupted-first.txt", 1000))}
        self.write_transcript([self.caller_row(),
                               self.message("msg-interrupted-first",
                                            [("toolu_interrupted_first", params)])])

        def interrupted(session_fd, name, record):
            raise OSError(errno.EIO, "injected interruption before the ledger rename")

        with mock.patch.object(guard, "_write_record", side_effect=interrupted):
            first = self.decide("toolu_interrupted_first", params)
        self.assertIn("Read admission", first)
        state = self.state_directory()
        self.assertFalse(self.record_path("msg-interrupted-first").exists())
        continuity = state / guard.READ_SESSION_NAME
        after_first = continuity.read_bytes()
        self.assertEqual(list(json.loads(after_first)["bindings"]), ["toolu_interrupted_first"])
        # The interrupted attempt left a durable binding without its ledger. An
        # unfinished first reservation cannot be told apart from a ledger lost after
        # an accepted reservation, so the retry stops explicitly with the surviving
        # evidence preserved instead of completing an unprovable reservation.
        reason = self.decide("toolu_interrupted_first", params)
        self.assertIn("ledger is missing", reason)
        self.assertIn("operator review", reason)
        self.assertEqual(continuity.read_bytes(), after_first)
        self.assertFalse(self.record_path("msg-interrupted-first").exists())

    def test_directory_growth_is_checked_before_crossing_its_exact_bound(self):
        first = {"file_path": str(self.source("exact-cap-one.txt", 1000))}
        self.write_transcript([self.caller_row(),
                               self.message("msg-exact-cap-one", [("toolu_exact_cap_one", first)])])
        self.assertIsNone(self.decide("toolu_exact_cap_one", first))
        state = self.state_directory()
        self.assertEqual(sorted(os.listdir(state)),
                         sorted([guard.READ_LOCK_NAME, guard.READ_SESSION_NAME,
                                 guard._message_digest("msg-exact-cap-one") + ".json"]))
        second = {"file_path": str(self.source("exact-cap-two.txt", 1000))}
        self.rows.append(self.assistant_row("msg-exact-cap-two",
                                            [self.read_item("toolu_exact_cap_two", second)],
                                            identity="assistant-exact-cap-two"))
        self.write_transcript()
        # Exactly at the bound there is no room for the new ledger and the scratch
        # entry its atomic write occupies, so growth is refused before any entry is
        # created and the existing evidence is left in place.
        with mock.patch.object(guard, "READ_STATE_MAX_ENTRIES", 3):
            reason = self.decide("toolu_exact_cap_two", second)
        self.assertIn("documented bound", reason)
        self.assertIn("scratch", reason)
        self.assertFalse(self.record_path("msg-exact-cap-two").exists())
        self.assertEqual(len(os.listdir(state)), 3)
        # One free entry is exactly enough for the new ledger and its live scratch
        # peak, and the directory never crosses the bound even transiently.
        peaks = []
        original_replace = guard.os.replace

        def counting_replace(src, dst, *args, **kwargs):
            peaks.append(len(os.listdir(state)))
            return original_replace(src, dst, *args, **kwargs)

        with mock.patch.object(guard.os, "replace", side_effect=counting_replace), \
                mock.patch.object(guard, "READ_STATE_MAX_ENTRIES", 4):
            self.assertIsNone(self.decide("toolu_exact_cap_two", second))
        self.assertEqual(peaks, [4, 4])
        self.assertEqual(len(os.listdir(state)), 4)
        # Retained scratch evidence counts against the same bound: at the bound, one
        # more entry for the existing message is refused and the evidence is kept.
        retained = state / (guard.READ_SCRATCH_PREFIX + "retained-evidence")
        retained.write_bytes(b"interrupted reservation evidence")
        third = {"file_path": str(self.source("exact-cap-three.txt", 1000))}
        self.rows.append(self.assistant_row("msg-exact-cap-two",
                                            [self.read_item("toolu_exact_cap_three", third)],
                                            identity="assistant-exact-cap-three"))
        self.write_transcript()
        with mock.patch.object(guard, "READ_STATE_MAX_ENTRIES", 5):
            reason = self.decide("toolu_exact_cap_three", third)
        self.assertIn("documented bound", reason)
        self.assertIn("scratch", reason)
        self.assertEqual(retained.read_bytes(), b"interrupted reservation evidence")
        self.assertEqual(len(self.record("msg-exact-cap-two")["entries"]), 1)
        self.assertEqual(self.record("msg-exact-cap-two")["total_raw_bytes"], 1000)

    def test_failed_directory_durability_is_never_admitted_by_an_idempotent_retry(self):
        params = {"file_path": str(self.source("durability.txt", 1000))}
        self.write_transcript([self.caller_row(),
                               self.message("msg-durability", [("toolu_durability", params)])])
        record_name = guard._message_digest("msg-durability") + ".json"
        original = guard.os.fsync
        blocked = []

        def flaky(fd):
            metadata = os.fstat(fd)
            if stat.S_ISDIR(metadata.st_mode):
                try:
                    target = self.state_directory().stat()
                except OSError:
                    target = None
                if target is not None and (target.st_dev, target.st_ino) == (metadata.st_dev,
                                                                             metadata.st_ino):
                    try:
                        os.stat(record_name, dir_fd=fd)
                        present = True
                    except FileNotFoundError:
                        present = False
                    if present:
                        blocked.append(fd)
                        raise OSError(errno.EIO, "injected directory durability failure")
            return original(fd)

        with mock.patch.object(guard.os, "fsync", side_effect=flaky):
            first = self.decide("toolu_durability", params)
            second = self.decide("toolu_durability", params)
        self.assertIn("directory durability", first)
        self.assertIn("directory durability", second)
        self.assertEqual(len(blocked), 2)
        self.assertTrue(self.record_path("msg-durability").exists())
        self.assertIsNone(self.decide("toolu_durability", params))
        self.assertEqual(self.record("msg-durability")["total_raw_bytes"], 1000)

    def test_unsupported_directory_durability_fails_closed_with_a_truthful_reason(self):
        params = {"file_path": str(self.source("unsupported-durability.txt", 100))}
        self.write_transcript([self.caller_row(),
                               self.message("msg-unsupported", [("toolu_unsupported", params)])])
        original = guard.os.fsync

        def unsupported(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError(errno.ENOTSUP, "directory fsync unsupported")
            return original(fd)

        with mock.patch.object(guard.os, "fsync", side_effect=unsupported):
            reason = self.decide("toolu_unsupported", params)
        self.assertIn("directory durability", reason)
        self.assertIn("could not confirm", reason)
        self.assertFalse(self.record_path("msg-unsupported").exists())

    def test_interrupted_namespace_creation_refuses_and_is_not_repaired_by_deletion(self):
        params = {"file_path": str(self.source("namespace.txt", 100))}
        self.write_transcript([self.caller_row(),
                               self.message("msg-namespace", [("toolu_namespace", params)])])
        original = guard._fsync_directory
        calls = []

        def failing(directory):
            calls.append(directory)
            if len(calls) == 1:
                raise OSError(errno.EIO, "injected parent durability failure")
            return original(directory)

        with mock.patch.object(guard, "_fsync_directory", side_effect=failing):
            reason = self.decide("toolu_namespace", params)
        self.assertIn("Read admission", reason)
        self.assertEqual(len(calls), 1)
        self.assertTrue((self.project / guard.READ_STATE_DIR).exists())
        self.assertFalse(self.record_path("msg-namespace").exists())
        self.assertIsNone(self.decide("toolu_namespace", params))
        self.assertEqual(self.record("msg-namespace")["total_raw_bytes"], 100)

    def test_streamed_same_message_rows_share_one_aggregate_budget(self):
        tool_uses = [("toolu_streamed_%02d" % index,
                      {"file_path": str(self.source("streamed-%02d.txt" % index, 5000))})
                     for index in range(14)]
        rows = [self.caller_row()]
        for start, count in ((0, 5), (5, 5), (9, 5)):
            rows.append(self.assistant_row("msg-streamed",
                                           [self.read_item(tool_id, params)
                                            for tool_id, params in tool_uses[start:start + count]],
                                           identity="assistant-streamed-%d" % start))
        self.write_transcript(rows)
        decisions = [self.decide(tool_id, params) for tool_id, params in tool_uses]
        admitted = [index for index, reason in enumerate(decisions) if reason is None]
        self.assertEqual(admitted, list(range(13)))
        self.assertIn("per-message admission budget", decisions[13])
        record = self.record("msg-streamed")
        self.assertEqual(record["total_raw_bytes"], 65000)
        self.assertEqual(len(record["entries"]), 13)
        ledgers = [name for name in os.listdir(self.state_directory())
                   if name.endswith(".json") and name != guard.READ_SESSION_NAME]
        self.assertEqual(ledgers, [guard._message_digest("msg-streamed") + ".json"])

    def test_framing_limit_refuses_the_call_without_charging_it(self):
        path = self.worktree / "framing.txt"
        path.write_bytes(bytes([10]) * 20000)
        refused = {"file_path": str(path), "offset": 1, "limit": 20000}
        admitted = {"file_path": str(path), "offset": 1, "limit": 4000}
        self.write_transcript([self.caller_row(), self.message("msg-framing", [
            ("toolu_framing_refused", refused), ("toolu_framing_admitted", admitted)])])
        reason = self.decide("toolu_framing_refused", refused)
        self.assertIn("framing", reason)
        self.assertIsNone(self.decide("toolu_framing_admitted", admitted))
        record = self.record("msg-framing")
        self.assertEqual(sorted(record["entries"]), ["toolu_framing_admitted"])
        self.assertEqual(record["total_raw_bytes"], 4000)

    def test_binding_capacity_stop_preserves_bindings_and_ledgers(self):
        first = {"file_path": str(self.source("binding-cap-one.txt", 1000))}
        self.write_transcript([self.caller_row(),
                               self.message("msg-binding-cap-one",
                                            [("toolu_binding_cap_one", first)])])
        with mock.patch.object(guard, "READ_STATE_MAX_BINDINGS", 1):
            self.assertIsNone(self.decide("toolu_binding_cap_one", first))
            second = {"file_path": str(self.source("binding-cap-two.txt", 1000))}
            self.rows.append(self.assistant_row("msg-binding-cap-two",
                                                [self.read_item("toolu_binding_cap_two", second)],
                                                identity="assistant-binding-cap-two"))
            self.write_transcript()
            reason = self.decide("toolu_binding_cap_two", second)
        self.assertIn("binding", reason)
        self.assertIn("capacity", reason)
        self.assertFalse(self.record_path("msg-binding-cap-two").exists())
        self.assertTrue(self.record_path("msg-binding-cap-one").exists())
        continuity = json.loads((self.state_directory() / guard.READ_SESSION_NAME).read_text())
        self.assertEqual(list(continuity["bindings"]), ["toolu_binding_cap_one"])

    def test_contended_lock_wait_is_bounded_and_writes_nothing(self):
        first = {"file_path": str(self.source("lock-wait-one.txt", 1000))}
        self.write_transcript([self.caller_row(),
                               self.message("msg-lock-wait-one",
                                            [("toolu_lock_wait_one", first)])])
        self.assertIsNone(self.decide("toolu_lock_wait_one", first))
        state = self.state_directory()
        ready = self.root / "lock-holder-ready"
        script = chr(10).join([
            "import fcntl, os, sys, time",
            "fd = os.open(sys.argv[1], os.O_RDONLY)",
            "fcntl.flock(fd, fcntl.LOCK_EX)",
            "open(sys.argv[2], 'w').close()",
            "time.sleep(5)"])
        holder = subprocess.Popen([sys.executable, "-I", "-c", script,
                                   str(state / guard.READ_LOCK_NAME), str(ready)],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 10
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists(), "lock holder did not become ready")
            second = {"file_path": str(self.source("lock-wait-two.txt", 1000))}
            self.rows.append(self.assistant_row("msg-lock-wait-two",
                                                [self.read_item("toolu_lock_wait_two", second)],
                                                identity="assistant-lock-wait-two"))
            self.write_transcript()
            with mock.patch.object(guard, "READ_LOCK_WAIT_SECONDS", 0.05):
                reason = self.decide("toolu_lock_wait_two", second)
            self.assertIn("lock", reason)
            self.assertIn("bounded", reason)
            self.assertFalse(self.record_path("msg-lock-wait-two").exists())
            self.assertEqual(list(json.loads(
                (state / guard.READ_SESSION_NAME).read_text())["bindings"]),
                ["toolu_lock_wait_one"])
            self.assertEqual(len(os.listdir(state)), 3)
        finally:
            holder.kill()
            holder.wait()

    # -- scope and export ---------------------------------------------------
    def test_admission_applies_only_to_the_root_read_tool(self):
        huge = {"file_path": str(self.source("scope.txt", guard.READ_MAX_SELECTED_BYTES + 1))}
        worker = self.event("toolu_scope_worker", huge)
        worker.update(agent_type="pr-sonnet", agent_id="native-child")
        self.assertIsNone(guard.decide(worker))
        ambiguous = self.event("toolu_scope_ambiguous", huge)
        ambiguous["agent_type"] = "pr-sonnet"
        self.assertIn("ambiguous worker identity", guard.decide(ambiguous))
        for tool in ("Grep", "Glob", "TaskOutput"):
            event = self.event("toolu_scope_" + tool, huge)
            event["tool_name"] = tool
            self.assertIsNone(guard.decide(event))
        small = {"file_path": str(self.source("scope-small.txt", 10))}
        self.write_transcript([self.caller_row(),
                               self.message("msg-scope", [("toolu_scope_read", small)])])
        self.assertIsNone(self.decide("toolu_scope_read", small))

    def test_guard_exposes_the_read_admission_version_and_budgets(self):
        self.assertEqual(guard.READ_ADMISSION_VERSION, 1)
        self.assertEqual(guard.READ_MAX_SELECTED_BYTES, 32_768)
        self.assertEqual(guard.READ_MAX_MESSAGE_BYTES, 65_536)


if __name__ == "__main__":
    unittest.main()
