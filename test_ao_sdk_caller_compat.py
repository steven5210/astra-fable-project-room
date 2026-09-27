"""Offline compatibility tests for the external SDK caller dialect and the bounded
Read readiness wait; every transcript, target file and hook event here is synthetic.

The tests exercise the deny-only guard directly. They use no model, provider,
credential, network, live native transcript or private room state, and they claim
nothing about live native hook compatibility. They were written without executing
the suite.
"""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import ao_routing_guard as guard


class SdkCallerAndReadinessTests(unittest.TestCase):
    """One synthetic prepared root engineer session with fake native records."""

    SESSION = "00000000-0000-4000-8000-000000000001"
    PROMPT_ONE = "11111111-1111-4111-8111-111111111111"
    PROMPT_TWO = "22222222-2222-4222-8222-222222222222"
    PROMPT_THREE = "33333333-3333-4333-8333-333333333333"

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
    def record(self, kind, identity, content, **fields):
        row = {"type": kind, "uuid": identity, "sessionId": self.session, "cwd": str(self.worktree),
               "isSidechain": False, "timestamp": "2026-01-01T00:00:00.000Z",
               "message": {"role": kind, "content": content}}
        row.update(fields)
        return row

    def legacy_caller(self, identity="caller-legacy", text="Continue the authorized work."):
        return self.record("user", identity, [{"type": "text", "text": text}],
                           origin={"kind": "human"})

    def sdk_caller(self, prompt_id, identity="caller-sdk", text="Apply the authorized correction.",
                   **fields):
        # Observed external SDK caller dialect: no origin field, promptSource=sdk,
        # turnOrigin=sdk, userType=external, entrypoint=sdk-cli and a promptId UUID.
        return self.record("user", identity, [{"type": "text", "text": text}],
                           promptSource="sdk", turnOrigin="sdk", userType="external",
                           entrypoint="sdk-cli", promptId=prompt_id, version="2.1.282",
                           permissionMode="auto", **fields)

    def notification_row(self, prompt_id, identity="task-notification"):
        # A harness notification in an SDK session can carry the same turn fields; the
        # synthetic envelope text is what excludes it from caller identity.
        return self.sdk_caller(
            prompt_id, identity=identity,
            text="<task-notification>\nA background task finished.\n</task-notification>")

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

    def message(self, message_id, tool_uses, identity="assistant-record"):
        return self.assistant_row(
            message_id, [self.read_item(tool_id, params) for tool_id, params in tool_uses],
            identity=identity)

    def quota_error(self, identity="quota-error"):
        row = self.record("assistant", identity, [{"type": "text", "text":
            "You've hit your session limit · resets 7:50pm (America/Los_Angeles)"}],
            isApiErrorMessage=True, error="rate_limit",
            quotaLimits={"status": "rejected", "rateLimitType": "five_hour"})
        row["message"].update(model="<synthetic>", stop_reason="stop_sequence")
        return row

    def write_transcript(self, rows=None):
        if rows is not None:
            self.rows = rows
        self.transcript.write_text("".join(json.dumps(row) + "\n" for row in self.rows))
        self.transcript.chmod(0o600)

    def source(self, name, size):
        path = self.worktree / name
        whole, rest = divmod(size, 40)
        path.write_bytes((b"x" * 39 + b"\n") * whole + b"x" * rest)
        return path

    def event(self, tool_use_id, params, **fields):
        event = {"tool_name": "Read", "tool_input": params, "session_id": self.session,
                 "transcript_path": str(self.transcript), "cwd": str(self.worktree),
                 "tool_use_id": tool_use_id}
        event.update(fields)
        return event

    def agent_event(self, **fields):
        event = {"tool_name": "Agent",
                 "tool_input": {"subagent_type": "pr-opus", "prompt": "Complete the bounded task.",
                                "description": "Synthetic task"},
                 "session_id": self.session, "transcript_path": str(self.transcript),
                 "cwd": str(self.worktree)}
        event.update(fields)
        return event

    def state_directory(self):
        return self.project / guard.READ_STATE_DIR / self.session

    def record_path(self, message_id):
        return self.state_directory() / (guard._message_digest(message_id) + ".json")

    # -- external SDK caller recognition ------------------------------------
    def test_current_external_sdk_caller_is_recognized_and_prompt_bound(self):
        self.write_transcript([self.sdk_caller(self.PROMPT_ONE)])
        proof = guard.inspect_quota(self.agent_event(prompt_id=self.PROMPT_ONE))
        self.assertEqual(proof, {"status": "clear_current_turn", "caller_uuid": "caller-sdk",
                                 "error_uuids": []})
        self.assertIsNone(guard.decide(self.agent_event(prompt_id=self.PROMPT_ONE)))
        # An older hook event that carries no prompt_id is still supported: the dialect
        # is positively certified, but its prompt binding cannot be enforced.
        self.assertIsNone(guard.decide(self.agent_event()))

    def test_sdk_caller_after_quota_opens_a_new_window_and_mismatch_refuses(self):
        self.write_transcript([self.sdk_caller(self.PROMPT_ONE), self.quota_error(),
                               self.sdk_caller(self.PROMPT_TWO, identity="caller-sdk-two")])
        proof = guard.inspect_quota(self.agent_event(prompt_id=self.PROMPT_TWO))
        self.assertEqual(proof, {"status": "clear_current_turn", "caller_uuid": "caller-sdk-two",
                                 "error_uuids": []})
        self.assertIsNone(guard.decide(self.agent_event(prompt_id=self.PROMPT_TWO)))
        with self.assertRaisesRegex(ValueError, "does not match the prompt_id"):
            guard.inspect_quota(self.agent_event(prompt_id=self.PROMPT_ONE))
        with self.assertRaisesRegex(ValueError, "does not match the prompt_id"):
            guard.inspect_quota(self.agent_event(prompt_id=self.PROMPT_THREE))

    def test_missing_origin_alone_is_never_a_caller(self):
        partial = self.sdk_caller(self.PROMPT_ONE, identity="no-origin")
        for name in ("origin", "promptId", "turnOrigin"):
            partial.pop(name, None)
        partial["entrypoint"] = "sdk-ts"
        self.write_transcript([self.legacy_caller(), self.quota_error(), partial])
        self.assertEqual(guard.inspect_quota(self.agent_event())["status"], "quota_in_current_turn")
        self.write_transcript([partial])
        with self.assertRaisesRegex(ValueError, "current human caller"):
            guard.inspect_quota(self.agent_event())

    def test_notification_summary_tool_result_internal_and_nested_records_are_not_callers(self):
        shapes = [
            ("notification", self.notification_row(self.PROMPT_TWO)),
            ("compaction", self.sdk_caller(self.PROMPT_TWO, identity="summary",
                                           isCompactSummary=True)),
            ("meta", self.sdk_caller(self.PROMPT_TWO, identity="meta", isMeta=True)),
            ("visible-only", self.sdk_caller(self.PROMPT_TWO, identity="visible",
                                             isVisibleInTranscriptOnly=True)),
            ("tool-result", self.record("user", "tool-result",
                                        [{"type": "tool_result", "tool_use_id": "toolu_any",
                                          "content": "task output"}],
                                        promptSource="sdk", turnOrigin="sdk", userType="external",
                                        entrypoint="sdk-cli", promptId=self.PROMPT_TWO,
                                        toolUseResult={"stdout": "task output"})),
            ("system", self.record("system", "internal", [{"type": "text", "text": "internal"}],
                                   promptSource="sdk", turnOrigin="sdk", userType="external",
                                   entrypoint="sdk-cli", promptId=self.PROMPT_TWO)),
            ("sidechain", self.sdk_caller(self.PROMPT_TWO, identity="nested", isSidechain=True)),
            ("nested-agent", self.sdk_caller(self.PROMPT_TWO, identity="nested-agent",
                                             agentId="native-child")),
        ]
        for label, shape in shapes:
            with self.subTest(shape=label):
                self.write_transcript([self.legacy_caller(), self.quota_error(), shape])
                proof = guard.inspect_quota(self.agent_event(prompt_id=self.PROMPT_TWO))
                self.assertEqual(proof["caller_uuid"], "caller-legacy")
                self.assertEqual(proof["status"], "quota_in_current_turn")

    def test_incomplete_or_conflicting_caller_identity_refuses_instead_of_falling_back(self):
        partial = self.sdk_caller(self.PROMPT_TWO, identity="partial-dialect")
        partial["entrypoint"] = "sdk-ts"
        missing = self.sdk_caller(self.PROMPT_TWO, identity="missing-prompt")
        missing.pop("promptId")
        malformed = self.sdk_caller("not-a-uuid", identity="malformed-prompt")
        conflicted = self.sdk_caller(self.PROMPT_TWO, identity="origin-conflict")
        conflicted["origin"] = {"kind": "agent"}
        string_origin = self.sdk_caller(self.PROMPT_TWO, identity="string-origin")
        string_origin["origin"] = "human"
        for label, shape in (("partial-dialect", partial), ("missing-prompt", missing),
                             ("malformed-prompt", malformed), ("origin-conflict", conflicted),
                             ("string-origin", string_origin)):
            with self.subTest(shape=label):
                # An older recognized caller record is present, so a silent skip would
                # falsely clear the turn instead of refusing the contradictory record.
                self.write_transcript([self.legacy_caller(), shape])
                with self.assertRaises(ValueError):
                    guard.inspect_quota(self.agent_event(prompt_id=self.PROMPT_TWO))
        with self.assertRaisesRegex(ValueError, "not a UUID"):
            guard.inspect_quota(self.agent_event(prompt_id="not-a-uuid"))
        with self.assertRaisesRegex(ValueError, "present but empty"):
            guard.inspect_quota(self.agent_event(prompt_id=""))

    def test_legacy_caller_stays_supported_when_the_hook_supplies_a_prompt_id(self):
        # Deliberately preserved older dialect: it carries no prompt binding, so the
        # supplied hook prompt_id cannot be enforced against it.
        self.write_transcript([self.legacy_caller()])
        self.assertIsNone(guard.decide(self.agent_event(prompt_id=self.PROMPT_TWO)))
        legacy_with_prompt = self.legacy_caller(identity="legacy-prompt")
        legacy_with_prompt["promptId"] = self.PROMPT_ONE
        self.write_transcript([legacy_with_prompt])
        self.assertIsNone(guard.decide(self.agent_event(prompt_id=self.PROMPT_ONE)))
        with self.assertRaisesRegex(ValueError, "does not match the prompt_id"):
            guard.inspect_quota(self.agent_event(prompt_id=self.PROMPT_TWO))

    # -- stale Read detection through SDK caller recognition -----------------
    def test_external_sdk_caller_makes_an_older_read_stale(self):
        params = {"file_path": str(self.source("sdk-stale.txt", 1000))}
        self.write_transcript([self.legacy_caller(),
                               self.message("msg-old", [("toolu_old", params)]),
                               self.sdk_caller(self.PROMPT_TWO, identity="caller-sdk-two")])
        self.assertIn("stale", guard.decide(self.event("toolu_old", params)))
        self.assertFalse(self.state_directory().exists())

    def test_auxiliary_records_after_the_read_do_not_make_it_stale(self):
        params = {"file_path": str(self.source("aux-after.txt", 100))}
        queue = {"type": "queue-operation", "operation": "enqueue", "sessionId": self.session}
        self.write_transcript([self.legacy_caller(),
                               self.message("msg-aux", [("toolu_aux", params)]),
                               self.notification_row(self.PROMPT_TWO), queue])
        self.assertIsNone(guard.decide(self.event("toolu_aux", params)))
        self.assertTrue(self.record_path("msg-aux").exists())

    def test_conflicting_caller_record_after_the_read_refuses_instead_of_admitting(self):
        params = {"file_path": str(self.source("conflict-after.txt", 100))}
        broken = self.sdk_caller(self.PROMPT_TWO, identity="broken-dialect")
        broken["entrypoint"] = "sdk-ts"
        self.write_transcript([self.legacy_caller(),
                               self.message("msg-conflict-after",
                                            [("toolu_conflict_after", params)]),
                               broken])
        reason = guard.decide(self.event("toolu_conflict_after", params))
        self.assertIn("Read admission", reason)
        self.assertIn("entrypoint", reason)
        self.assertFalse(self.state_directory().exists())

    def test_read_path_does_not_bind_the_hook_prompt_id(self):
        # Deliberate, documented limit: the Read anchor is the requesting assistant
        # record, not the caller, so a hook prompt_id is not enforced there.
        params = {"file_path": str(self.source("read-prompt-id.txt", 100))}
        self.write_transcript([self.sdk_caller(self.PROMPT_ONE),
                               self.message("msg-read-prompt", [("toolu_read_prompt", params)])])
        self.assertIsNone(guard.decide(self.event("toolu_read_prompt", params,
                                                  prompt_id=self.PROMPT_TWO)))

    # -- bounded foreground readiness for the native flush race --------------
    def test_late_valid_append_is_admitted_within_the_bounded_wait(self):
        params = {"file_path": str(self.source("late-append.txt", 1000))}
        self.write_transcript([self.legacy_caller()])
        sleeps = []

        def late_append(seconds):
            sleeps.append(seconds)
            self.assertFalse(self.state_directory().exists())
            self.rows.append(self.message("msg-late", [("toolu_late", params)]))
            self.write_transcript()

        with mock.patch.object(guard.time, "sleep", side_effect=late_append):
            self.assertIsNone(guard.decide(self.event("toolu_late", params)))
        self.assertEqual(len(sleeps), 1)
        record = json.loads(self.record_path("msg-late").read_text())
        self.assertEqual(record["total_raw_bytes"], 1000)
        self.assertEqual(list(record["entries"]), ["toolu_late"])

    def test_permanently_absent_read_fails_closed_within_finite_bounds(self):
        params = {"file_path": str(self.source("never-there.txt", 1000))}
        self.write_transcript([self.legacy_caller()])
        sleeps = []
        with mock.patch.object(guard.time, "sleep",
                               side_effect=lambda seconds: sleeps.append(seconds)), \
                mock.patch.object(guard, "READ_CORRELATION_ATTEMPTS", 3), \
                mock.patch.object(guard, "READ_CORRELATION_WAIT_SECONDS", 30.0), \
                mock.patch.object(guard, "READ_CORRELATION_SLEEP_SECONDS", 0.0):
            reason = guard.decide(self.event("toolu_missing", params))
        self.assertIn("3 bounded checks", reason)
        self.assertIn("could not be positively correlated", reason)
        self.assertIn("operator review", reason)
        self.assertEqual(len(sleeps), 2)
        self.assertFalse(self.state_directory().exists())

    def test_partial_tail_conflicts_and_staleness_are_final_and_never_retried(self):
        params = {"file_path": str(self.source("final-errors.txt", 100))}
        self.write_transcript([self.legacy_caller(),
                               self.message("msg-final", [("toolu_final", params)])])
        with self.transcript.open("ab") as stream:
            stream.write(b'{"type":"assistant","message":{"id":"msg-unfinished"')
        with mock.patch.object(guard.time, "sleep",
                               side_effect=AssertionError("a partial tail was retried")):
            self.assertIn("still incomplete", guard.decide(self.event("toolu_final", params)))
        self.write_transcript([self.legacy_caller(),
                               self.message("msg-final", [("toolu_final", params)]),
                               self.message("msg-final",
                                            [("toolu_final", {"file_path": "/other"})],
                                            identity="assistant-conflicting")])
        with mock.patch.object(guard.time, "sleep",
                               side_effect=AssertionError("a conflict was retried")):
            self.assertIn("different Read arguments",
                          guard.decide(self.event("toolu_final", params)))
        self.write_transcript([self.legacy_caller(),
                               self.message("msg-final", [("toolu_final", params)]),
                               self.assistant_row("msg-newer", [], identity="assistant-newer")])
        with mock.patch.object(guard.time, "sleep",
                               side_effect=AssertionError("staleness was retried")):
            self.assertIn("stale", guard.decide(self.event("toolu_final", params)))
        self.assertFalse(self.state_directory().exists())

    def test_late_conflicting_duplicate_is_refused_not_admitted(self):
        params = {"file_path": str(self.source("late-duplicate.txt", 1000))}
        self.write_transcript([self.legacy_caller()])

        def late_duplicate(seconds):
            self.rows.append(self.message("msg-late-duplicate",
                                          [("toolu_late_duplicate", params)]))
            self.rows.append(self.assistant_row(
                "msg-late-duplicate",
                [self.read_item("toolu_late_duplicate", {"file_path": "/other"})],
                identity="assistant-late-duplicate"))
            self.write_transcript()

        with mock.patch.object(guard.time, "sleep", side_effect=late_duplicate):
            reason = guard.decide(self.event("toolu_late_duplicate", params))
        self.assertIn("different Read arguments", reason)
        self.assertFalse(self.state_directory().exists())

    def test_late_append_that_arrives_stale_is_refused(self):
        params = {"file_path": str(self.source("late-stale.txt", 1000))}
        self.write_transcript([self.legacy_caller()])

        def late_stale(seconds):
            self.rows.append(self.message("msg-late-stale", [("toolu_late_stale", params)]))
            self.rows.append(self.sdk_caller(self.PROMPT_TWO, identity="caller-after"))
            self.write_transcript()

        with mock.patch.object(guard.time, "sleep", side_effect=late_stale):
            reason = guard.decide(self.event("toolu_late_stale", params))
        self.assertIn("stale", reason)
        self.assertFalse(self.state_directory().exists())

    def test_transcript_replacement_while_waiting_is_not_a_late_append(self):
        params = {"file_path": str(self.source("replacement-wait.txt", 1000))}
        self.write_transcript([self.legacy_caller()])
        replacement = self.root / "replacement-transcript.jsonl"

        def swap(seconds):
            replacement.write_text("".join(json.dumps(row) + "\n" for row in (
                self.legacy_caller(),
                self.message("msg-replacement-wait", [("toolu_replacement_wait", params)]))))
            replacement.chmod(0o600)
            os.replace(replacement, self.transcript)

        with mock.patch.object(guard.time, "sleep", side_effect=swap):
            reason = guard.decide(self.event("toolu_replacement_wait", params))
        self.assertIn("was replaced", reason)
        self.assertIn("operator review", reason)
        self.assertFalse(self.state_directory().exists())

    def test_transcript_rewind_while_waiting_is_refused(self):
        params = {"file_path": str(self.source("rewind-wait.txt", 1000))}
        filler = {"type": "queue-operation", "operation": "enqueue", "sessionId": self.session,
                  "pad": "x" * 8192}
        self.write_transcript([self.legacy_caller(), filler])

        def rewind(seconds):
            self.write_transcript([self.legacy_caller()])

        with mock.patch.object(guard.time, "sleep", side_effect=rewind):
            reason = guard.decide(self.event("toolu_rewind_wait", params))
        self.assertIn("truncated or rewritten", reason)
        self.assertFalse(self.state_directory().exists())

    def test_under_lock_revalidation_stays_strict_after_the_wait(self):
        params = {"file_path": str(self.source("under-lock.txt", 1000))}
        self.write_transcript([self.legacy_caller()])
        original = guard._measure_read

        def late_append(seconds):
            self.rows.append(self.message("msg-under-lock", [("toolu_under_lock", params)]))
            self.write_transcript()

        def partial_after_measure(path, offset, limit):
            result = original(path, offset, limit)
            with self.transcript.open("ab") as stream:
                stream.write(b'{"type":"user","uuid":"caller-late"')
            return result

        with mock.patch.object(guard.time, "sleep", side_effect=late_append), \
                mock.patch.object(guard, "_measure_read", side_effect=partial_after_measure):
            reason = guard.decide(self.event("toolu_under_lock", params))
        self.assertIn("still incomplete", reason)
        self.assertFalse(self.record_path("msg-under-lock").exists())

    def test_no_reservation_is_written_before_positive_identity(self):
        params = {"file_path": str(self.source("no-early-reservation.txt", 100))}
        self.write_transcript([self.legacy_caller()])
        with mock.patch.object(guard, "_write_state_record",
                               side_effect=AssertionError("state written without identity")), \
                mock.patch.object(guard.os, "mkdir",
                                  side_effect=AssertionError("namespace created without identity")), \
                mock.patch.object(guard.time, "sleep", side_effect=lambda seconds: None), \
                mock.patch.object(guard, "READ_CORRELATION_ATTEMPTS", 2), \
                mock.patch.object(guard, "READ_CORRELATION_WAIT_SECONDS", 30.0):
            reason = guard.decide(self.event("toolu_no_reservation", params))
        self.assertIn("could not be positively correlated", reason)
        self.assertFalse(self.state_directory().exists())

    def test_readiness_wait_does_not_bypass_the_per_read_budget(self):
        oversized = self.worktree / "late-oversized.txt"
        oversized.write_bytes(b"x" * (guard.READ_MAX_SELECTED_BYTES + 1024))
        params = {"file_path": str(oversized)}
        self.write_transcript([self.legacy_caller()])

        def late_append(seconds):
            self.rows.append(self.message("msg-late-oversized",
                                          [("toolu_late_oversized", params)]))
            self.write_transcript()

        with mock.patch.object(guard.time, "sleep", side_effect=late_append):
            reason = guard.decide(self.event("toolu_late_oversized", params))
        self.assertIn("per-read admission budget", reason)
        self.assertFalse(self.state_directory().exists())

    def test_readiness_bounds_are_small_and_documented_budgets_are_unchanged(self):
        self.assertGreaterEqual(guard.READ_CORRELATION_ATTEMPTS, 1)
        self.assertLessEqual(guard.READ_CORRELATION_ATTEMPTS, 16)
        self.assertGreater(guard.READ_CORRELATION_WAIT_SECONDS, 0.0)
        self.assertLessEqual(guard.READ_CORRELATION_WAIT_SECONDS, 5.0)
        self.assertGreater(guard.READ_CORRELATION_SLEEP_SECONDS, 0.0)
        self.assertLess(guard.READ_CORRELATION_SLEEP_SECONDS, guard.READ_CORRELATION_WAIT_SECONDS)
        self.assertEqual(guard.READ_ADMISSION_VERSION, 1)
        self.assertEqual(guard.READ_MAX_SELECTED_BYTES, 32_768)
        self.assertEqual(guard.READ_MAX_MESSAGE_BYTES, 65_536)
        self.assertEqual(guard.READ_MAX_MESSAGE_FRAMED_BYTES,
                         guard.READ_MAX_MESSAGE_BYTES + guard.READ_MAX_SELECTED_BYTES)

    # -- corrected Read readiness and argument-shape regressions -------------
    def test_wait_refuses_grow_then_rewind_below_latest_absent_revision(self):
        params = {"file_path": str(self.source("grow-then-rewind.txt", 1000))}
        self.write_transcript([self.legacy_caller()])
        steps = []

        def grow_then_rewind(seconds):
            steps.append(self.transcript.stat().st_size)
            if len(steps) == 1:
                self.rows.append({"type": "queue-operation", "operation": "enqueue",
                                  "sessionId": self.session, "pad": "x" * 9000})
                self.write_transcript()
                return
            self.write_transcript([self.legacy_caller(),
                                   self.message("msg-grow-then-rewind",
                                                [("toolu_grow_then_rewind", params)])])

        with mock.patch.object(guard.time, "sleep", side_effect=grow_then_rewind):
            reason = guard.decide(self.event("toolu_grow_then_rewind", params))
        self.assertIn("truncated or rewritten below the revision already inspected", reason)
        self.assertFalse(self.record_path("msg-grow-then-rewind").exists())
        self.assertFalse(self.state_directory().exists())

    def test_wait_refuses_same_inode_rewrite_of_previous_content_evidence(self):
        params = {"file_path": str(self.source("rewrite-larger.txt", 1000))}
        self.write_transcript([self.legacy_caller(text="ORIGINAL caller text.")])

        def rewrite_larger(seconds):
            self.write_transcript([self.legacy_caller(text="A DIFFERENT caller text."),
                                   self.message("msg-rewrite-larger",
                                                [("toolu_rewrite_larger", params)])])

        with mock.patch.object(guard.time, "sleep", side_effect=rewrite_larger):
            reason = guard.decide(self.event("toolu_rewrite_larger", params))
        self.assertIn("bounded previous-content evidence", reason)
        self.assertFalse(self.record_path("msg-rewrite-larger").exists())
        self.assertFalse(self.state_directory().exists())

    def test_wait_deadline_refuses_a_late_resumed_success(self):
        params = {"file_path": str(self.source("deadline-late.txt", 1000))}
        absent = guard._ReadCallAbsent("absent", 1, 2, 100)
        clock = [0.0]

        def oversleep(seconds):
            clock[0] = 10.0

        with mock.patch.object(guard, "_read_message_identity",
                               side_effect=[absent, ("msg-late", 1, 2, 200)]), \
                mock.patch.object(guard.time, "sleep", side_effect=oversleep), \
                mock.patch.object(guard.time, "monotonic", side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(guard._ReadRefused,
                                        "could not be positively correlated"):
                guard._read_message_identity_with_readiness(
                    str(self.transcript), self.session, str(self.worktree),
                    "toolu_deadline", params)
        self.assertFalse(self.state_directory().exists())

    def test_hook_prompt_id_null_empty_and_non_text_are_not_omission(self):
        self.write_transcript([self.sdk_caller(self.PROMPT_ONE)])
        self.assertIsNone(guard.decide(self.agent_event()))
        for label, value, expected in (
                ("null", None, "present but null"),
                ("empty", "", "present but empty"),
                ("non-text", 7, "present but not text"),
                ("malformed", "not-a-uuid", "not a UUID")):
            with self.subTest(prompt_id=label):
                with self.assertRaisesRegex(ValueError, expected):
                    guard.inspect_quota(self.agent_event(prompt_id=value))

    def test_explicit_null_origin_is_refused_and_legacy_prompt_id_still_binds(self):
        explicit_null = self.sdk_caller(self.PROMPT_ONE, identity="origin-null")
        explicit_null["origin"] = None
        self.write_transcript([self.legacy_caller(), explicit_null])
        with self.assertRaisesRegex(ValueError, "explicit null origin"):
            guard.inspect_quota(self.agent_event(prompt_id=self.PROMPT_ONE))
        legacy = self.legacy_caller(identity="legacy-bound")
        legacy["promptId"] = self.PROMPT_ONE
        self.write_transcript([legacy])
        self.assertIsNone(guard.decide(self.agent_event(prompt_id=self.PROMPT_ONE)))


if __name__ == "__main__":
    unittest.main()
