"""Offline native-envelope recovery tests. No account, network, subprocess model or inference."""

import copy
import json
from unittest.mock import patch

import ao_project_room as ao
import ao_response_normalization as normalization
import ao_workflow
import deepseek_adapter
import project_room
import project_room_mcp
import test_ao_continuation as continuation
from test_ao_normal import Fixture, DelegateFixture


class ResponseNormalizationTests(Fixture):
    def setUp(self):
        super().setUp()
        self.room = self.open()
        self.spec()
        self.bind()

    def spec_result(self, **changes):
        spec = self.service.spec(self.directory(), self.state())
        return {"interpretation": "The synthetic fixture contract is understood.", "findings": [], "decision": "accept",
                "spec_revision": spec["revision"], "spec_sha256": spec["sha256"], **changes}

    def finish_spec(self, value=None, prefix="Review complete. The fixture contract is grounded and I found no blockers.\n\n```json\n", suffix="\n```"):
        self.send("spec_review")
        self.fake.finish("engineer", prefix + json.dumps(value or self.spec_result()) + suffix)
        self.service.ao_room_sync(self.room)

    def finish_engineering(self, **changes):
        self.agree()
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.send("implementation")
        (self.repo / "feature.txt").write_text("implemented\n")
        self.fake.finish("engineer", "The unit is complete. The delegate derived the fixtures; final engineering report follows.\n\n"
                         + json.dumps(self.report(**changes)))
        self.service.ao_room_sync(self.room)

    def args(self, request_id="spec_review", **changes):
        request = self.state()["requests"][request_id]
        raw = ao_workflow.raw_final_text(self.directory(), request)
        start = raw.find("{")
        # Fixture spans use the standard decoder so tests can submit ambiguous/non-finite objects to the real validator.
        _, end = json.JSONDecoder().raw_decode(raw, start)
        return {"room_id": self.room, "request_id": request_id, "receipt_sha256": request["receipt_sha256"],
                "final_text_sha256": ao.digest(raw.encode()), "json_start": start, "json_end": end,
                "astra_review": "I read the complete final response and all surrounding prose. It adds no additional or contradictory verdict; the exact object contains the complete outcome.",
                "confirm_no_additional_verdict": True, **changes}

    def normalize(self, request_id="spec_review", **changes):
        return self.service.ao_room_response_normalize(**self.args(request_id, **changes))

    def files(self):
        return {str(p.relative_to(self.directory())): p.read_bytes() for p in self.directory().rglob("*") if p.is_file()}

    def assert_refuses_unchanged(self, kwargs=None, pattern=None):
        before, posts = self.files(), copy.deepcopy(self.fake.posts)
        with self.assertRaises(ao.RoomError) if pattern is None else self.assertRaisesRegex(ao.RoomError, pattern):
            self.service.ao_room_response_normalize(**(kwargs or self.args()))
        self.assertEqual(self.files(), before)
        self.assertEqual(self.fake.posts, posts)

    def test_fenced_spec_envelope_needs_explicit_review_and_preserves_native_bytes_and_budget(self):
        self.finish_spec()
        self.assertFalse(self.service.ao_room_status(self.room)["agreement"]["agreed"])
        state = self.state(); request = copy.deepcopy(state["requests"]["spec_review"])
        receipt = (self.directory() / request["receipt"]).read_bytes()
        result = self.normalize()
        self.assertTrue(result["normalized"]); self.assertFalse(result["model_dispatch"])
        self.assertTrue(self.service.ao_room_status(self.room)["agreement"]["agreed"])
        after = self.state()["requests"]["spec_review"]
        self.assertEqual({k: after[k] for k in request}, request)
        self.assertEqual((self.directory() / request["receipt"]).read_bytes(), receipt)
        self.assertEqual(len(self.fake.posts), 1)
        self.assertEqual(sum(r.get("purpose") == "spec_review" for r in self.state()["requests"].values()), 1)
        record = ao.read(self.directory() / after["response_normalization"])
        self.assertIn("Review complete.", record["outside_text"]["before"])
        self.assertEqual(record["outside_text"]["after"], "\n```")

    def test_raw_engineering_envelope_captures_candidate_before_parse_and_can_verify_without_retry(self):
        self.finish_engineering()
        request = self.state()["requests"]["implementation"]
        original = copy.deepcopy(request)
        self.assertIn("engineering_error", request)
        self.assertIn("completion_candidate", request)
        self.assertNotIn("engineering_record", request)
        self.normalize("implementation")
        after = self.state()["requests"]["implementation"]
        self.assertEqual({k: after[k] for k in original}, original)
        self.assertIn("engineering_record", after)
        self.assertTrue(self.service.ao_room_verify(self.room, str(self.repo))["passed"])
        self.assertEqual(len(self.fake.posts), 2)
        self.assertEqual(self.state()["acceptances"], [])

    def test_idempotent_exact_input_and_changed_review_refusal(self):
        self.finish_spec(); args = self.args()
        result = self.service.ao_room_response_normalize(**args)
        before = self.files()
        self.assertEqual(self.service.ao_room_response_normalize(**args), result)
        self.assertEqual(self.files(), before)
        self.assert_refuses_unchanged({**args, "astra_review": "Different disposition"}, "different immutable")

    def test_no_automatic_semantic_prose_inference_or_opt_out(self):
        self.finish_spec(prefix="Do not treat the following JSON as a verdict.\n", suffix="")
        self.assertFalse(self.service.ao_room_status(self.room)["agreement"]["agreed"])
        self.assert_refuses_unchanged(self.args(confirm_no_additional_verdict=False), "explicitly confirm")
        self.assert_refuses_unchanged(self.args(astra_review=""))
        # Whether prose is contradictory is the explicit operator judgment boundary, never guessed by a word denylist.

    def test_exact_receipt_and_untrimmed_unicode_text_digests_are_required(self):
        self.finish_spec(prefix="  Résumé of fixture review.\n", suffix="\n ")
        self.assert_refuses_unchanged(self.args(receipt_sha256="0" * 64), "exact receipt")
        self.assert_refuses_unchanged(self.args(final_text_sha256="0" * 64), "exact receipt")
        self.assert_refuses_unchanged(self.args(json_start=self.args()["json_start"] + 1), "first and only")
        self.assertTrue(self.normalize()["normalized"])

    def test_duplicate_keys_nonfinite_numbers_and_multiple_objects_refuse(self):
        samples = [
            '{"decision":"changes_required","decision":"accept"}',
            '{"nested":{"same":1,"same":2}}',
            '{"value":NaN}', '{"value":Infinity}', '{"value":-Infinity}', '{"value":1e9999}',
            '{"a":1}\n{"b":2}', '[{"a":1}]', '"{"a":1}"',
            '```json\n{"a":1}\n```\n```json\n{"b":2}\n```',
        ]
        for text in samples:
            with self.subTest(text=text):
                start = text.find("{")
                _, end = json.JSONDecoder().raw_decode(text, start)
                with self.assertRaises(ao.RoomError):
                    normalization.extract(text, start, end)

    def test_nested_object_cherry_picking_and_incomplete_spans_refuse(self):
        raw = 'Preamble.\n{"outer":{"decision":"accept"}}'
        start = raw.index('{"decision"')
        _, end = json.JSONDecoder().raw_decode(raw, start)
        with self.assertRaisesRegex(ao.RoomError, "first and only"):
            normalization.extract(raw, start, end)
        self.finish_spec()
        self.assert_refuses_unchanged(self.args(json_end=self.args()["json_end"] - 1), "complete top-level")

    def test_supported_rejection_and_blocker_remain_nonagreement(self):
        for index, change in enumerate(({"decision": "changes_required"}, {"findings": ["BLOCKER: fixture is missing"]})):
            request_id = "review-" + str(index)
            self.send("spec_review", request_id)
            self.fake.finish("engineer", "Final review follows.\n" + json.dumps(self.spec_result(**change)))
            self.service.ao_room_sync(self.room)
            self.normalize(request_id)
            self.assertFalse(self.service.ao_room_status(self.room)["agreement"]["agreed"])
            with self.assertRaises(ao.RoomError):
                self.service.ao_room_handoff(self.room, str(self.repo))

    def test_normalized_scope_change_blocks_correction_through_shared_reader(self):
        self.finish_engineering(outcome="scope_change", implementation_complete=False)
        self.normalize("implementation")
        before = len(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, "scope change"):
            self.send("correction")
        self.assertEqual(len(self.fake.posts), before)

    def test_normalized_incomplete_report_cannot_pass_engineering_ready(self):
        self.finish_engineering(implementation_complete=False, remaining_gaps=["One required case remains"])
        self.normalize("implementation")
        with self.assertRaisesRegex(ao.RoomError, "incomplete|remaining gaps"):
            self.service.ao_room_verify(self.room, str(self.repo))

    def test_changed_or_historical_uncaptured_candidate_refuses_without_recapture(self):
        self.finish_engineering()
        args = self.args("implementation")
        (self.repo / "feature.txt").write_text("different\n")
        self.assert_refuses_unchanged(args, "Candidate changed")
        (self.repo / "feature.txt").write_text("implemented\n")
        state = self.state(); request = state["requests"]["implementation"]
        for key in ("completion_candidate", "completion_candidate_sha256", "completion_capture_attempted"):
            request.pop(key, None)
        ao.atomic(self.directory() / "state.json", state)
        self.assert_refuses_unchanged(args, "candidate-at-completion")

    def test_failed_initial_candidate_capture_never_gets_retried_by_normalization(self):
        self.agree(); self.service.ao_room_handoff(self.room, str(self.repo)); self.send("implementation")
        self.fake.finish("engineer", "Final report follows.\n" + json.dumps(self.report()))
        with patch.object(ao_workflow, "candidate_snapshot", side_effect=ao_workflow.ImplementationError("snapshot unavailable")):
            self.service.ao_room_sync(self.room)
        request = self.state()["requests"]["implementation"]
        self.assertTrue(request["completion_capture_attempted"])
        self.assertNotIn("completion_candidate", request)
        self.assert_refuses_unchanged(self.args("implementation"), "candidate-at-completion")

    def test_late_normalization_capture_failure_keeps_original_error_and_blocks_acceptance(self):
        self.finish_engineering()
        original = self.state()["requests"]["implementation"]["engineering_error"]
        with patch.object(ao_workflow, "candidate_snapshot", side_effect=ao_workflow.ImplementationError("late snapshot failure")):
            self.normalize("implementation")
        request = self.state()["requests"]["implementation"]
        self.assertEqual(request["engineering_error"], original)
        self.assertEqual(request["normalization_capture_error"], "late snapshot failure")
        self.assertIn("response_normalization", request)
        self.assertNotIn("engineering_record", request)
        with self.assertRaisesRegex(ao.RoomError, "captured completed"):
            self.service.ao_room_verify(self.room, str(self.repo))

    def test_modified_completion_candidate_and_raw_receipt_refuse(self):
        self.finish_engineering()
        args = self.args("implementation"); request = self.state()["requests"]["implementation"]
        path = self.directory() / request["completion_candidate"]
        original = path.read_bytes(); path.write_text("{}")
        self.assert_refuses_unchanged(args, "Completion candidate evidence")
        path.write_bytes(original)
        receipt = self.directory() / request["receipt"]
        receipt.write_text("{}")
        self.assert_refuses_unchanged(args, "receipt was modified")

    def test_modified_normalization_blocks_all_future_consumers(self):
        self.finish_spec(); self.normalize()
        request = self.state()["requests"]["spec_review"]
        path = self.directory() / request["response_normalization"]
        record = ao.read(path); record["outside_text"]["before"] = "Changed outside prose"
        ao.atomic(path, record)
        with self.assertRaisesRegex(ao.RoomError, "normalization evidence was modified"):
            self.service.ao_room_handoff(self.room, str(self.repo))
        self.assertEqual(len(self.fake.posts), 1)

    def test_orphan_normalization_file_refuses_instead_of_overwriting(self):
        self.finish_spec()
        ao.atomic(self.directory() / "response-normalizations/spec_review.json", {"unclaimed": True})
        self.assert_refuses_unchanged(pattern="Unclaimed response")

    def test_latest_role_response_and_current_spec_required(self):
        self.finish_spec(); args = self.args()
        self.send("spec_review", "newer")
        self.fake.finish("engineer", "New malformed final"); self.service.ao_room_sync(self.room)
        self.assert_refuses_unchanged(args, "latest response")
        self.spec(2)
        self.assert_refuses_unchanged(args, "stale specification")

    def test_incomplete_native_work_and_identity_contradictions_refuse(self):
        self.finish_spec(); args = self.args()
        self.fake.snapshots["engineer"]["turns"][-1]["state"] = "running"
        self.assert_refuses_unchanged(args, "active work")
        self.fake.snapshots["engineer"]["turns"][-1]["state"] = "completed"
        self.fake.snapshots["engineer"]["settings"]["model"] = "opus"
        self.assert_refuses_unchanged(args, "model/effort changed")

    def test_receipt_identity_streaming_tail_and_truncated_history_refuse(self):
        self.finish_spec()
        original_state = self.state(); original_request = original_state["requests"]["spec_review"]
        receipt_path = self.directory() / original_request["receipt"]; original_receipt = ao.read(receipt_path)
        mutations = [
            lambda r: r.update(history_truncated=True),
            lambda r: r["turn"].update(providerTurnId="another-provider-turn"),
            lambda r: r["turn"].update(state="failed"),
            lambda r: r["messages"][-1].update(turnId="another-turn"),
            lambda r: r["messages"].append({"id": "streaming-tail", "role": "assistant", "turnId": r["turn"]["id"], "text": "Incomplete replacement", "streaming": True}),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                state = copy.deepcopy(original_state); receipt = copy.deepcopy(original_receipt); mutate(receipt)
                ao.atomic(receipt_path, receipt)
                state["requests"]["spec_review"]["receipt_sha256"] = ao.digest(receipt)
                ao.atomic(self.directory() / "state.json", state)
                self.assert_refuses_unchanged(self.args())

    def test_valid_plain_and_whole_fenced_json_remain_compatible(self):
        for raw in ('{"a":1}', ' \n```json\n{"a":1}\n```\n'):
            self.assertEqual(normalization.strict_object(raw), {"a": 1})
        self.agree()
        self.assert_refuses_unchanged(pattern="already strict JSON")

    def test_new_out_of_band_turn_and_live_response_drift_refuse(self):
        self.finish_spec(); args = self.args()
        snapshot = self.fake.snapshots["engineer"]
        snapshot["turns"].append({"id": "unowned", "providerTurnId": "unowned-native", "state": "completed"})
        self.assert_refuses_unchanged(args, "stale, incomplete")
        snapshot["turns"].pop()
        snapshot["messages"][-1]["text"] += "\nAdditional final finding."
        self.assert_refuses_unchanged(args, "stale, incomplete")

    def test_acceptance_envelope_keeps_exact_review_checkpoint_and_rejection(self):
        self.agree(); self.implement(); self.service.ao_room_verify(self.room, str(self.repo))
        for index, decision in enumerate(("rejected", "approved")):
            request_id = "accept-" + str(index)
            self.send("acceptance_review", request_id)
            request = self.state()["requests"][request_id]
            value = {**request["review"], "decision": decision, "review": "Independently inspected the synthetic candidate and all gate evidence."}
            self.fake.finish("reviewer", "Independent review is complete.\n" + json.dumps(value))
            self.service.ao_room_sync(self.room)
            with self.assertRaises(ao.RoomError):
                self.service.ao_room_accept(self.room, request_id)
            self.normalize(request_id)
            self.assertEqual(self.state()["acceptances"], [])
            if decision == "rejected":
                with self.assertRaisesRegex(ao.RoomError, "rejected"):
                    self.service.ao_room_accept(self.room, request_id)
            else:
                self.assertTrue(self.service.ao_room_accept(self.room, request_id)["accepted"])
        self.assertEqual(len(self.fake.posts), 4)

    def test_stale_reviewer_gate_or_candidate_evidence_refuses(self):
        self.agree(); self.implement(); self.service.ao_room_verify(self.room, str(self.repo)); self.send("acceptance_review")
        request = self.state()["requests"]["acceptance_review"]
        self.fake.finish("reviewer", "Review follows.\n" + json.dumps({**request["review"], "decision": "approved", "review": "Passed"}))
        self.service.ao_room_sync(self.room)
        args = self.args("acceptance_review")
        checkpoint = self.service.checkpoint(self.directory(), self.state())
        gate = self.directory() / checkpoint["gates"][0]["log"]
        gate.write_text("Modified gate evidence")
        self.assert_refuses_unchanged(args, "Verification evidence")

    def test_mcp_surface_exposes_audited_operation_without_dispatch(self):
        self.finish_spec()
        service = project_room.Service(self.home)
        with patch.object(ao.Service, "__init__", lambda obj, home: setattr(obj, "root", self.home / "ao")), patch.object(ao.Service, "client", lambda obj, state: self.fake):
            response = project_room_mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "ao_room_response_normalize", "arguments": self.args()}}, service)
        self.assertTrue(response["result"]["structuredContent"]["normalized"])
        self.assertEqual(len(self.fake.posts), 1)


class ProviderResponseNormalizationTests(DelegateFixture):
    JOB = 'a' * 32
    job = continuation.DelegateEvidenceTests.job
    args = ResponseNormalizationTests.args
    normalize = ResponseNormalizationTests.normalize
    files = ResponseNormalizationTests.files
    assert_refuses_unchanged = ResponseNormalizationTests.assert_refuses_unchanged

    def setUp(self):
        super().setUp()
        self.bind(); self.agree()
        self.service.ao_room_handoff(self.room, str(self.repo)); self.send("implementation")
        self.ledger = deepseek_adapter.Ledger(self.home)

    def finish(self):
        (self.repo / "feature.txt").write_text("implemented\n")
        self.fake.finish("engineer", "The delegated fixture work is complete. Final report follows.\n" +
                         json.dumps(self.report(routing_log=[{"delegate_job_ids": [self.JOB]}])))
        self.service.ao_room_sync(self.room)

    def test_normalized_provider_report_retains_exact_job_and_content_verification(self):
        output = self.job(); self.finish(); self.normalize("implementation")
        request = self.state()["requests"]["implementation"]
        record = ao.read(self.directory() / request["engineering_record"])
        self.assertEqual(record["delegation"][0]["classification"], "completed")
        self.assertTrue(self.service.ao_room_verify(self.room, str(self.repo))["passed"])
        output.write_text("Different delegate answer")
        with self.assertRaises(ao.RoomError):
            self.service.ao_room_verify(self.room, str(self.repo))
        self.assertEqual(len(self.fake.posts), 2)

    def test_foreign_and_unsettled_delegates_refuse_normalization(self):
        self.job(room="other-room"); self.finish()
        self.assert_refuses_unchanged(self.args("implementation"))
        with self.ledger.transaction() as db:
            db.execute("UPDATE jobs SET room_id=?, state=? WHERE id=?", (self.room, deepseek_adapter.STREAMING, self.JOB))
        self.assert_refuses_unchanged(self.args("implementation"), "still active")

    def test_lost_provider_ledger_is_never_recreated_by_normalization(self):
        self.job(); self.finish()
        ledger = self.home / "deepseek/ledger.sqlite3"; ledger.unlink()
        self.assert_refuses_unchanged(self.args("implementation"), "ledger is missing")
        self.assertFalse(ledger.exists())
