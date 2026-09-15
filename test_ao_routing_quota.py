"""Offline native quota routing tests; every transcript and hook event is synthetic.

The observed SDK field shapes are reproduced without private caller bytes, real
session IDs, account state, credentials, provider processes, or network access.
"""

import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import ao_routing_guard as guard


class NativeQuotaGuardTests(unittest.TestCase):
    def setUp(self):
        foreground = mock.patch.dict(os.environ, {"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"})
        foreground.start(); self.addCleanup(foreground.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        self.session = "00000000-0000-4000-8000-000000000001"
        self.project = self.root / "claude" / "projects" / "synthetic-project"
        self.project.mkdir(parents=True)
        self.transcript = self.project / (self.session + ".jsonl")
        self.write([self.caller()])

    def record(self, kind, identity, content, **fields):
        value = {"type": kind, "uuid": identity, "parentUuid": "previous-native-event",
                 "sessionId": self.session, "cwd": str(self.worktree), "isSidechain": False,
                 "timestamp": "2026-01-01T00:00:00.000Z",
                 "message": {"role": kind, "content": content}}
        value.update(fields)
        return value

    def caller(self, identity="caller-one", text="Apply the authorized correction.", **fields):
        # Exact observed root SDK caller shape, with synthetic identity and text.
        return self.record("user", identity, [{"type": "text", "text": text}],
                           origin={"kind": "human"}, promptSource="sdk", userType="external",
                           entrypoint="sdk-ts", permissionMode="auto", **fields)

    def error(self, identity="quota-error", **fields):
        # Exact observed native rejection shape: the child failure was followed by
        # this typed root API error before a subsequent Opus Agent call.
        value = self.record("assistant", identity, [{"type": "text", "text":
            "You've hit your session limit · resets 7:50pm (America/Los_Angeles)"}],
            isApiErrorMessage=True, error="rate_limit", apiErrorStatus=429,
            quotaLimits={"status": "rejected", "resetsAt": 1, "rateLimitType": "five_hour",
                         "unifiedRateLimitFallbackAvailable": False, "overageStatus": "rejected",
                         "overageDisabledReason": "org_level_disabled", "isUsingOverage": False})
        value["message"].update(model="<synthetic>", stop_reason="stop_sequence")
        value.update(fields)
        return value

    def tool_result(self, **fields):
        return self.record("user", "tool-result", [{"type": "tool_result", "tool_use_id": "sonnet-task",
            "is_error": True, "content": "You've hit your session limit · resets 7:50pm (America/Los_Angeles)"}], **fields)

    def event(self, tool="Agent", **fields):
        params = {"subagent_type": "pr-opus", "prompt": "Complete the bounded task.", "description": "Synthetic task"}
        if tool != "Agent":
            params = {"task": "Synthetic task"}
        return {"tool_name": tool, "tool_input": params, "session_id": self.session,
                "transcript_path": str(self.transcript), "cwd": str(self.worktree), **fields}

    def write(self, records):
        self.transcript.write_text("".join(json.dumps(record) + "\n" for record in records))
        self.transcript.chmod(0o600)

    def inspect(self, **fields):
        return guard.inspect_quota(self.event(**fields))

    def ambiguous_error_identities(self, error):
        rows = []
        for cwd in (None, str(self.root), str(self.worktree).upper(), str(self.worktree) + "/.",
                    str(self.root) + "//worktree"):
            row = copy.deepcopy(error); row["cwd"] = cwd; rows.append(row)
        for model in (None, "<Synthetic>", "claude-fable-test"):
            row = copy.deepcopy(error); row["message"]["model"] = model; rows.append(row)
        row = copy.deepcopy(error); row.pop("cwd"); rows.append(row)
        row = copy.deepcopy(error); row["message"].pop("model"); rows.append(row)
        row = copy.deepcopy(error); row.pop("cwd"); row["message"].pop("model"); rows.append(row)
        return rows

    def test_recorded_sonnet_failure_root_quota_then_opus_sequence(self):
        launch = self.record("assistant", "sonnet-launch", [{"type": "tool_use", "name": "Agent",
            "id": "sonnet-task", "input": {"subagent_type": "pr-sonnet"}}])
        self.write([self.caller(), launch, self.tool_result()])
        self.assertIsNone(guard.decide(self.event()))  # A tool's text is not native typed quota evidence.
        self.write([self.caller(), launch, self.tool_result(), self.error()])
        proof = self.inspect()
        self.assertEqual(proof, {"status": "quota_in_current_turn", "caller_uuid": "caller-one",
                                 "error_uuids": ["quota-error"]})
        for name in ("pr-opus", "pr-sonnet"):
            event = self.event()
            event["tool_input"]["subagent_type"] = name
            self.assertIn("quota was rejected", guard.decide(event))
        for tool in ("mcp__deepseek__deepseek_submit", "mcp__deepseek__deepseek_ask"):
            self.assertIn("quota was rejected", guard.decide(self.event(tool)))

    def test_typed_account_windows_establish_quota_without_text_heuristics(self):
        for window in ("five_hour", "seven_day"):
            error = self.error(quotaLimits={"status": "rejected", "rateLimitType": window})
            error["message"]["content"] = [{"type": "text", "text": "Native account rejection."}]
            self.write([self.caller(), error])
            self.assertEqual(self.inspect()["status"], "quota_in_current_turn")

    def test_native_session_message_fallback_without_newer_quota_metadata(self):
        for text in ("You've hit your session limit", "You've hit your account limit",
                     "You've hit your session limit · resets 7:50pm (America/Los_Angeles)"):
            error = self.error()
            error.pop("quotaLimits")
            error["message"]["content"] = [{"type": "text", "text": text}]
            self.write([self.caller(), error])
            self.assertIsNotNone(guard.decide(self.event()))

    def test_generic_429_per_model_limits_and_unknown_scopes_do_not_create_shared_stop(self):
        errors = []
        generic = self.error()
        generic.pop("quotaLimits")
        generic["message"]["content"] = [{"type": "text", "text": "429 rate_limit: Sonnet requests per minute exceeded"}]
        errors.append(generic)
        for window in ("seven_day_opus", "seven_day_sonnet", "requests_per_minute", "unknown_window"):
            errors.append(self.error(quotaLimits={"status": "rejected", "rateLimitType": window}))
        errors.append(self.error(quotaLimits={"status": "allowed", "rateLimitType": "five_hour"}))
        for error in errors:
            self.write([self.caller(), error])
            self.assertEqual(self.inspect()["status"], "clear_current_turn")
            self.assertIsNone(guard.decide(self.event()))

    def test_quoted_or_untyped_text_never_establishes_quota(self):
        quote = "You've hit your session limit · resets 7:50pm (America/Los_Angeles)"
        untyped = self.error(isApiErrorMessage=False)
        wrong_role = self.error()
        wrong_role["message"]["role"] = "user"
        other_error = self.error(error="max_output_tokens")
        nested = self.record("assistant", "nested", [{"type": "text", "text": json.dumps(self.error())}])
        for record in (self.tool_result(), untyped, wrong_role, other_error, nested):
            self.write([self.caller(text=quote), record])
            self.assertEqual(self.inspect()["status"], "clear_current_turn")
        error = self.error()
        error.pop("quotaLimits")
        for text in ("Quoted: " + quote, quote + "\nDo this next", "You've hit your model limit", "HTTP 429"):
            error["message"]["content"] = text
            self.write([self.caller(), error])
            self.assertIsNone(guard.decide(self.event()))

    def test_only_exact_session_root_errors_count(self):
        foreign = [{"sessionId": "00000000-0000-4000-8000-000000000002"},
                   {"sessionId": None}, {"sessionId": "urn:uuid:" + self.session}, {"isSidechain": True},
                   {"isSidechain": "false"}, {"agentId": "child"}, {"agent_id": "child"}]
        for fields in foreign:
            for error in (self.error(**fields), *self.ambiguous_error_identities(self.error(**fields))):
                self.write([self.caller(), error])
                self.assertEqual(self.inspect()["status"], "clear_current_turn")

    def test_positive_account_evidence_with_ambiguous_cwd_or_model_blocks_unverified(self):
        errors = [self.error(quotaLimits={"status": "rejected", "rateLimitType": window})
                  for window in ("five_hour", "seven_day")]
        for error in errors:
            error["message"]["content"] = "Typed account rejection without fallback wording."
        fallback = self.error(); fallback.pop("quotaLimits"); errors.append(fallback)
        for account in errors:
            for error in self.ambiguous_error_identities(account):
                with self.subTest(quota=error.get("quotaLimits"), cwd=error.get("cwd"), model=error["message"].get("model")):
                    self.write([self.caller(), error])
                    with self.assertRaisesRegex(ValueError, "account quota evidence has unverified"):
                        self.inspect()
                    for tool in guard.NEW_WORK_TOOLS:
                        with self.assertRaisesRegex(ValueError, "account quota evidence has unverified"):
                            guard.decide(self.event(tool))

    def test_ambiguous_identity_does_not_promote_generic_or_per_model_limits(self):
        generic = self.error(); generic.pop("quotaLimits")
        generic["message"]["content"] = "429 rate_limit: Sonnet requests per minute exceeded"
        untyped = self.error(isApiErrorMessage=False)
        errors = [generic, untyped, self.error(quotaLimits={"status": "allowed", "rateLimitType": "five_hour"})]
        for window in ("seven_day_opus", "seven_day_sonnet", "requests_per_minute", "unknown_window"):
            errors.append(self.error(quotaLimits={"status": "rejected", "rateLimitType": window}))
        for quota in ({}, []):
            errors.append(self.error(quotaLimits=quota))
        for other in errors:
            for error in self.ambiguous_error_identities(other):
                with self.subTest(quota=error.get("quotaLimits"), cwd=error.get("cwd"), model=error["message"].get("model")):
                    self.write([self.caller(), error])
                    self.assertEqual(self.inspect()["status"], "clear_current_turn")
                    self.assertIsNone(guard.decide(self.event()))

    def test_new_true_human_continuation_has_a_new_window_without_mutating_history(self):
        for error in (self.error(), *self.ambiguous_error_identities(self.error())):
            self.write([self.caller(), error, self.caller("caller-two", "Continue once after the authorized reset.")])
            original = self.transcript.read_bytes()
            self.assertEqual(self.inspect(), {"status": "clear_current_turn", "caller_uuid": "caller-two", "error_uuids": []})
            self.assertIsNone(guard.decide(self.event()))
            self.assertEqual(self.transcript.read_bytes(), original)

    def test_summaries_tool_results_meta_and_nonhuman_text_do_not_clear_quota(self):
        fake_callers = [self.caller("summary", isCompactSummary=True),
                       self.caller("summary-visible", isVisibleInTranscriptOnly=True),
                       self.caller("meta", isMeta=True), self.caller("sidechain", isSidechain=True),
                       self.caller("other-session", sessionId="00000000-0000-4000-8000-000000000002"),
                       self.caller("other-cwd", cwd=str(self.root)),
                       self.tool_result(origin={"kind": "human"})]
        absent = self.caller("missing-origin")
        absent.pop("origin")
        fake_callers.append(absent)
        nonhuman = self.caller("nonhuman")
        nonhuman["origin"] = {"kind": "agent"}
        fake_callers.append(nonhuman)
        for caller in fake_callers:
            self.write([self.caller(), self.error(), caller])
            self.assertEqual(self.inspect()["status"], "quota_in_current_turn")

    def test_successful_end_turn_and_elapsed_reset_do_not_release_in_turn_quota(self):
        end = self.record("assistant", "root-end-turn", [{"type": "text", "text": "Paused."}])
        end["message"].update(model="claude-fable-test", stop_reason="end_turn")
        self.write([self.caller(), self.error(), end])
        self.assertIsNotNone(guard.decide(self.event()))  # Error's resetsAt=1 is deliberately long elapsed.

    def test_reads_results_cancellation_and_worker_execution_do_not_read_quota_transcript(self):
        tools = ("Read", "Glob", "Grep", "ToolSearch", "TaskOutput", "AskUserQuestion",
                 "mcp__deepseek__deepseek_health", "mcp__deepseek__deepseek_status",
                 "mcp__deepseek__deepseek_result", "mcp__deepseek__deepseek_cancel")
        with mock.patch.object(guard, "_transcript_tail", side_effect=AssertionError("unexpected evidence read")):
            for tool in tools:
                self.assertIsNone(guard.decide(self.event(tool, transcript_path="unsafe")))
            self.assertIsNone(guard.decide(self.event("Bash", transcript_path="unsafe",
                agent_type="pr-sonnet", agent_id="native-child")))
            self.assertIn("one layer", guard.decide(self.event(agent_type="pr-opus", agent_id="native-child")))

    def test_missing_metadata_is_explicitly_unavailable_and_partial_metadata_blocks(self):
        legacy = {"tool_name": "Agent", "tool_input": {"subagent_type": "pr-sonnet"}}
        self.assertEqual(guard.inspect_quota(legacy), {"status": "missing_hook_metadata"})
        self.assertIsNone(guard.decide(legacy))
        for field in guard.QUOTA_HOOK_FIELDS:
            with self.assertRaises(ValueError):
                guard.decide({**legacy, field: self.event()[field]})
        for fields in ({"session_id": "not-a-session"}, {"session_id": None}, {"session_id": 7},
                       {"transcript_path": None}, {"cwd": None}, {"cwd": "relative"}):
            with self.assertRaises(ValueError):
                self.inspect(**fields)

    def test_mismatched_root_filename_and_unsafe_paths_are_refused_before_read(self):
        paths = [str(self.project / "different.jsonl"), "relative/" + self.transcript.name,
                 str(self.project) + "/../" + self.transcript.name,
                 str(self.project) + "/./" + self.transcript.name,
                 str(self.project) + "//" + self.transcript.name,
                 str(self.project / self.session / "subagents" / "agent-child.jsonl")]
        with mock.patch.object(guard.os, "pread", side_effect=AssertionError("unsafe path was read")):
            for path in paths:
                with self.assertRaises(ValueError):
                    self.inspect(transcript_path=path)

    def test_incomplete_or_malformed_current_turn_refuses_but_old_bytes_are_not_interpreted(self):
        for raw in (b"", b"{}\n", b"[]\n", b"{broken}\n", b"{\"type\":", b"\xff\n"):
            self.transcript.write_bytes(raw)
            with self.assertRaises((ValueError, UnicodeDecodeError)):
                self.inspect()
        caller = self.caller()
        caller.pop("origin")
        self.write([caller])
        with self.assertRaisesRegex(ValueError, "current human caller"):
            self.inspect()
        self.write([self.caller()])
        self.transcript.write_bytes(b"malformed old bytes\n" + self.transcript.read_bytes())
        self.assertEqual(self.inspect()["status"], "clear_current_turn")

    def test_missing_native_identity_in_current_caller_or_quota_refuses(self):
        for record in (self.caller(), self.error()):
            record.pop("uuid")
            self.write([self.caller("earlier-caller"), record])
            with self.assertRaisesRegex(ValueError, "lacks its identity"):
                self.inspect()

    def test_large_old_history_is_bounded_and_a_current_caller_outside_bound_refuses(self):
        self.write([self.caller(), self.error()])
        current = self.transcript.read_bytes()
        # The bound begins halfway through an irrelevant old line.
        self.transcript.write_bytes(b"x" * 12_000 + b"\n" + current)
        with mock.patch.object(guard, "MAX_TRANSCRIPT_BYTES", len(current) + 100), \
                mock.patch.object(guard.os, "pread", wraps=os.pread) as reader:
            self.assertEqual(self.inspect()["status"], "quota_in_current_turn")
            self.assertLessEqual(reader.call_args.args[1], len(current) + 101)
        with mock.patch.object(guard, "MAX_TRANSCRIPT_BYTES", 20):
            with self.assertRaisesRegex(ValueError, "current caller window|current human caller"):
                self.inspect()

    def test_record_bound_cannot_silently_truncate_a_turn(self):
        noise = self.record("assistant", "read", [{"type": "text", "text": "Inspection."}])
        self.write([self.caller(), self.error()] + [noise] * 8)
        with mock.patch.object(guard, "MAX_TRANSCRIPT_RECORDS", 4):
            with self.assertRaisesRegex(ValueError, "within its bound"):
                self.inspect()

    def test_symlink_file_or_parent_is_not_followed(self):
        outside = self.root / "outside"
        outside.mkdir()
        target = outside / self.transcript.name
        shutil.copyfile(self.transcript, target)
        self.transcript.unlink()
        self.transcript.symlink_to(target)
        with mock.patch.object(guard.os, "pread", side_effect=AssertionError("symlink was read")):
            with self.assertRaises(OSError):
                self.inspect()
        self.transcript.unlink()
        self.project.rmdir()
        self.project.symlink_to(outside, target_is_directory=True)
        with mock.patch.object(guard.os, "pread", side_effect=AssertionError("symlink parent was read")):
            with self.assertRaises(OSError):
                self.inspect()

    def test_fifo_directory_hardlink_and_unsafe_modes_are_not_read(self):
        for shape in ("fifo", "directory", "hardlink", "writable"):
            self.transcript.unlink()
            if shape == "fifo":
                os.mkfifo(self.transcript, 0o600)
            elif shape == "directory":
                self.transcript.mkdir()
            else:
                self.transcript.write_bytes(b"SYNTHETIC_CANARY")
                if shape == "hardlink":
                    os.link(self.transcript, self.root / "hardlink")
                else:
                    self.transcript.chmod(0o666)
            with mock.patch.object(guard.os, "pread", side_effect=AssertionError("unsafe leaf was read")):
                with self.assertRaises(ValueError):
                    self.inspect()
            if shape == "directory":
                self.transcript.rmdir()
                self.transcript.write_bytes(b"placeholder")

    def test_foreign_owner_and_writable_parent_are_not_read(self):
        with mock.patch.object(guard.os, "getuid", return_value=os.getuid() + 1), \
                mock.patch.object(guard.os, "pread", side_effect=AssertionError("foreign file was read")):
            with self.assertRaises(ValueError):
                self.inspect()
        self.project.chmod(0o777)
        with mock.patch.object(guard.os, "pread", side_effect=AssertionError("shared parent was read")):
            with self.assertRaises(ValueError):
                self.inspect()

    def test_leaf_symlink_swap_at_open_is_not_followed(self):
        target = self.root / "target"
        target.write_bytes(b"SYNTHETIC_OUTSIDE_CANARY")
        original, swapped = guard._open_component, []

        def swap(name, flags, dir_fd):
            if name == self.transcript.name and not swapped:
                swapped.append(True)
                self.transcript.unlink()
                self.transcript.symlink_to(target)
            return original(name, flags, dir_fd)

        with mock.patch.object(guard, "_open_component", side_effect=swap), \
                mock.patch.object(guard.os, "pread", side_effect=AssertionError("swap was followed")):
            with self.assertRaises(OSError):
                self.inspect()
        self.assertTrue(swapped)

    def test_directory_swap_after_open_retains_the_bound_descriptor(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / self.transcript.name).write_bytes(b"SYNTHETIC_OUTSIDE_CANARY")
        original, swapped = guard._open_component, []

        def swap(name, flags, dir_fd):
            if name == self.transcript.name and not swapped:
                swapped.append(True)
                self.project.rename(self.project.with_name("original-project"))
                self.project.symlink_to(outside, target_is_directory=True)
            return original(name, flags, dir_fd)

        with mock.patch.object(guard, "_open_component", side_effect=swap):
            self.assertEqual(self.inspect()["status"], "clear_current_turn")
        self.assertTrue(swapped)

    def test_change_or_short_read_during_inspection_refuses(self):
        original = os.pread

        def append(fd, size, offset):
            raw = original(fd, size, offset)
            with self.transcript.open("ab") as stream:
                stream.write(b"{}\n")
            return raw

        with mock.patch.object(guard.os, "pread", side_effect=append):
            with self.assertRaisesRegex(ValueError, "changed during inspection"):
                self.inspect()
        with mock.patch.object(guard.os, "pread", return_value=b"short"):
            with self.assertRaisesRegex(ValueError, "changed during inspection"):
                self.inspect()

    def test_copied_alone_hook_denies_or_blocks_and_never_grants(self):
        script = self.root / "standalone-guard.py"
        shutil.copyfile(guard.__file__, script)
        run = lambda event: subprocess.run([sys.executable, "-I", str(script)],
            input=json.dumps(event), capture_output=True, text=True, cwd=self.worktree, timeout=10)
        clear = run(self.event())
        self.assertEqual((clear.returncode, clear.stdout, clear.stderr), (0, "", ""))
        self.write([self.caller(text="PRIVATE_SYNTHETIC_CALLER"), self.error()])
        denied = run(self.event())
        self.assertEqual(denied.returncode, 0)
        decision = json.loads(denied.stdout)["hookSpecificOutput"]
        self.assertEqual((decision["hookEventName"], decision["permissionDecision"]), ("PreToolUse", "deny"))
        self.assertNotIn("PRIVATE_SYNTHETIC_CALLER", denied.stdout + denied.stderr)
        self.write([self.caller(text="PRIVATE_SYNTHETIC_CALLER"), self.error(cwd=None)])
        for tool in guard.NEW_WORK_TOOLS:
            unverified = run(self.event(tool))
            self.assertEqual((unverified.returncode, unverified.stdout), (2, ""))
            self.assertIn("account quota evidence has unverified", unverified.stderr)
            self.assertNotIn("PRIVATE_SYNTHETIC_CALLER", unverified.stderr)
        inspected = run(self.event("mcp__deepseek__deepseek_result"))
        self.assertEqual((inspected.returncode, inspected.stdout, inspected.stderr), (0, "", ""))
        self.transcript.unlink()
        missing = run(self.event())
        self.assertEqual((missing.returncode, missing.stdout), (2, ""))
        legacy = self.event()
        for field in guard.QUOTA_HOOK_FIELDS:
            legacy.pop(field)
        unavailable = run(legacy)
        self.assertEqual((unavailable.returncode, unavailable.stdout), (0, ""))
        self.assertIn("evidence unavailable (missing hook metadata)", unavailable.stderr)


if __name__ == "__main__":
    unittest.main()
