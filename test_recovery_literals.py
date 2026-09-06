"""Independent literals from the pinned revision 2 spec for the recovery classifier (delegate-authored, Fable-verified)."""

import unittest

from recovery import classify_quota_result, parse_timeout_error


SID = "11111111-2222-4333-8444-555555555555"


def quota(**overrides):
    base = {"type": "result", "subtype": "success", "is_error": True, "terminal_reason": "api_error", "api_error_status": 429,
            "stop_reason": "stop_sequence", "session_id": SID,
            "result": "You've hit your session limit · resets 2pm (America/Los_Angeles)"}
    base.update(overrides)
    return base


class TestClassifyQuotaResult(unittest.TestCase):
    def test_exact_observed_example(self):
        self.assertEqual(classify_quota_result(quota(), SID), [])

    def test_empty_permission_denials(self):
        self.assertEqual(classify_quota_result(quota(permission_denials=[]), SID), [])

    def test_zero_usage_and_nonzero_model_usage(self):
        usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        self.assertEqual(classify_quota_result(quota(usage=usage, modelUsage={"claude-fable-5-1": {"inputTokens": 12345, "outputTokens": 678}}), SID), [])

    def test_missing_or_empty_usage_metadata(self):
        self.assertEqual(classify_quota_result(quota(), SID), [])
        self.assertEqual(classify_quota_result(quota(modelUsage={}), SID), [])

    def test_time_variants(self):
        for time in ("12am", "12:30pm", "1:05am", "11:59pm", "2pm"):
            with self.subTest(time=time):
                self.assertEqual(classify_quota_result(quota(result=f"You've hit your session limit · resets {time} (UTC)"), SID), [])

    def test_zone_variants(self):
        for zone in ("Europe/London", "UTC", "Etc/GMT+5", "America/Argentina/Buenos_Aires"):
            with self.subTest(zone=zone):
                self.assertEqual(classify_quota_result(quota(result=f"You've hit your session limit · resets 2pm ({zone})"), SID), [])

    def test_unknown_extra_keys_ignored(self):
        self.assertEqual(classify_quota_result(quota(duration_ms=5, num_turns=3, uuid="x"), SID), [])

    def test_result_text_variants_rejected(self):
        bad_texts = [
            "You've hit your session limit · resets 13pm (UTC)",
            "You've hit your session limit · resets 0am (UTC)",
            "You've hit your session limit · resets 2:60pm (UTC)",
            "You've hit your session limit · resets 2pm (UTC)\n",
            "You've hit your session limit · resets 2pm (UTC) please retry",
            "You've hit your session limit · resets 2pm ()",
            "You've hit your session limit · resets 2pm (" + "A" * 200 + ")",
            "You've hit your session limit - resets 2pm (UTC)",
            "You've hit your session limit . resets 2pm (UTC)",
            "You’ve hit your session limit · resets 2pm (UTC)",
            " You've hit your session limit · resets 2pm (UTC)",
            "You've hit your session limit · resets 2pm (UTC) ",
            "Not logged in · Please run /login", "Rate limited", "", None, 123]
        for text in bad_texts:
            with self.subTest(text=text):
                reasons = classify_quota_result(quota(result=text), SID)
                self.assertIn("result_text_mismatch", reasons)

    def test_is_error_not_true(self):
        for value in (False, "true", 1):
            with self.subTest(is_error=value):
                self.assertIn("is_error_not_true", classify_quota_result(quota(is_error=value), SID))

    def test_api_error_status_mismatch(self):
        for value in (500, 529, "429", 429.0, True):
            with self.subTest(status=value):
                self.assertIn("api_error_status_mismatch", classify_quota_result(quota(api_error_status=value), SID))

    def test_terminal_reason_mismatch(self):
        for value in ("completed", None):
            with self.subTest(terminal_reason=value):
                self.assertIn("terminal_reason_mismatch", classify_quota_result(quota(terminal_reason=value), SID))
        missing = quota()
        del missing["terminal_reason"]
        self.assertIn("terminal_reason_mismatch", classify_quota_result(missing, SID))

    def test_type_and_subtype_mismatch(self):
        self.assertIn("result_type_mismatch", classify_quota_result(quota(type="assistant"), SID))
        self.assertIn("result_subtype_mismatch", classify_quota_result(quota(subtype="error"), SID))

    def test_stop_reason_mismatch(self):
        self.assertIn("stop_reason_mismatch", classify_quota_result(quota(stop_reason="end_turn"), SID))
        missing = quota()
        del missing["stop_reason"]
        self.assertIn("stop_reason_mismatch", classify_quota_result(missing, SID))

    def test_session_mismatch(self):
        for value in ("99999999-9999-4999-8999-999999999999", None):
            with self.subTest(session_id=value):
                self.assertIn("session_mismatch", classify_quota_result(quota(session_id=value), SID))
        missing = quota()
        del missing["session_id"]
        self.assertIn("session_mismatch", classify_quota_result(missing, SID))

    def test_permission_denials_present(self):
        self.assertIn("permission_denials_present", classify_quota_result(quota(permission_denials=[{"tool_name": "Bash"}]), SID))

    def test_result_not_object(self):
        for value in ([], "text", None):
            with self.subTest(result=value):
                self.assertIn("result_not_object", classify_quota_result(value, SID))

    def test_quota_text_in_other_field_rejected(self):
        result = quota(result="Done", structured_output={"summary": "You've hit your session limit · resets 2pm (UTC)"})
        self.assertIn("result_text_mismatch", classify_quota_result(result, SID))


class TestParseTimeoutError(unittest.TestCase):
    def test_exact_saved_timeout(self):
        error = "TimeoutExpired: Command '['claude', '--print']' timed out after 3599.7900607919873 seconds"
        self.assertEqual(parse_timeout_error(error, ["claude", "--print"], 3600), (3599.7900607919873, []))

    def test_argv_with_quotes_and_braces(self):
        argv = ["claude", "--settings", '{"disableAllHooks":true}', "--session-id", SID]
        error = f"TimeoutExpired: Command '{argv}' timed out after 3599.5 seconds"
        self.assertEqual(parse_timeout_error(error, argv, 3600), (3599.5, []))

    def test_boundary_seconds(self):
        argv = ["claude", "--print"]
        for pinned_timeout, s_text, expected in ((2, "1.9999", 1.9999), (3600, "3600.0", 3600.0), (3600, "3540.0", 3540.0)):
            with self.subTest(pinned_timeout=pinned_timeout, s_text=s_text):
                error = f"TimeoutExpired: Command '{argv}' timed out after {s_text} seconds"
                self.assertEqual(parse_timeout_error(error, argv, pinned_timeout), (expected, []))

    def test_seconds_out_of_range(self):
        argv = ["claude", "--print"]
        for pinned_timeout, s_text, code in ((3600, "3600.01", "timeout_seconds_out_of_range"), (3600, "3539.9", "timeout_seconds_out_of_range"), (3600, "0", None)):
            with self.subTest(s_text=s_text):
                seconds, reasons = parse_timeout_error(f"TimeoutExpired: Command '{argv}' timed out after {s_text} seconds", argv, pinned_timeout)
                self.assertIsNone(seconds)
                self.assertTrue(reasons)
                if code is not None:
                    self.assertIn(code, reasons)

    def test_invalid_seconds(self):
        argv = ["claude", "--print"]
        for s_text in ("inf", "nan", "1e400", "-5", "abc", ""):
            with self.subTest(s_text=s_text):
                seconds, reasons = parse_timeout_error(f"TimeoutExpired: Command '{argv}' timed out after {s_text} seconds", argv, 3600)
                self.assertIsNone(seconds)
                self.assertTrue(reasons)
                if s_text in ("inf", "nan", "abc"):
                    self.assertIn("timeout_seconds_invalid", reasons)

    def test_wrong_argv_prefix(self):
        error = f"TimeoutExpired: Command '{['claude', '--print']}' timed out after 3599.9 seconds"
        seconds, reasons = parse_timeout_error(error, ["claude", "--print", "--x"], 3600)
        self.assertIsNone(seconds)
        self.assertIn("error_prefix_mismatch", reasons)

    def test_non_timeout_error_strings(self):
        cases = [("ImplementationError: Claude did not return a successful terminal result for the exact implementation session", ["claude", "--print"]),
                 ("InvocationTerminated: Received signal 15", ["claude", "--print"]),
                 ("TimeoutExpired: Command 'x' timed out after 5 seconds", ["x"]), ("", ["claude", "--print"])]
        for error, argv in cases:
            with self.subTest(error=error):
                seconds, reasons = parse_timeout_error(error, argv, 3600)
                self.assertIsNone(seconds)
                self.assertIn("error_prefix_mismatch", reasons)

    def test_trailing_after_seconds(self):
        argv = ["claude", "--print"]
        base_error = f"TimeoutExpired: Command '{argv}' timed out after 3599.9 seconds"
        for suffix in ("\n", " extra"):
            with self.subTest(suffix=suffix):
                seconds, reasons = parse_timeout_error(base_error + suffix, argv, 3600)
                self.assertIsNone(seconds)
                self.assertTrue(reasons)

    def test_injected_timeout_fragment_in_argv(self):
        argv = ["claude", "a' timed out after 1 seconds"]
        error = f"TimeoutExpired: Command '{argv}' timed out after 3599.9 seconds"
        self.assertEqual(parse_timeout_error(error, argv, 3600), (3599.9, []))


if __name__ == "__main__":
    unittest.main()
