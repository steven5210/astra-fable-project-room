"""Provider-none report repair and correction identity guards."""
import copy
import json
import unittest
from unittest.mock import patch

import ao_delegates
import ao_project_room as ao
import ao_workflow
from test_ao_normal import Fixture


class ProviderNoneReportCorrectionTests(Fixture):
    BAD = "native-agent-123"
    PROVIDER_ID = "a" * 32
    NO_PROVIDER_ERROR = "Engineering report names delegate jobs, but this room has no delegate provider"

    def setUp(self):
        super().setUp()
        self.room = self.open(provider="none")
        self.spec()
        self.bind()
        self.agree()
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.send("implementation")

    def complete_implementation(self, ids):
        (self.repo / "feature.txt").write_text("implemented\n")
        self.fake.finish("engineer", json.dumps(self.report(
            routing_log=[{"delegate_job_ids": ids}])))
        self.service.ao_room_sync(self.room)
        return self.state()["requests"]["implementation"]

    def send_correction(self):
        return self.service.ao_room_send(self.room, "engineer", "Continue.", "correction",
                                         purpose="correction")

    def complete_correction(self, ids):
        # The real shared producer binds this exact saved request's caller/model/owner/session and
        # timestamps, so the correction completion has its own native rows before the sync; the test
        # sends the correction directly instead of through Fixture.send, which notes them itself.
        self.note_native_turn("correction")
        self.fake.finish("engineer", json.dumps(self.report(
            routing_log=[{"delegate_job_ids": ids}])))
        self.service.ao_room_sync(self.room)
        return self.state()["requests"]["correction"]

    def assert_provider_shaped_refusal(self, ids):
        self.complete_implementation(ids)
        before = (self.directory() / "state.json").read_bytes()
        posts = copy.deepcopy(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, self.NO_PROVIDER_ERROR):
            self.send_correction()
        self.assertEqual((self.directory() / "state.json").read_bytes(), before)
        self.assertEqual(self.fake.posts, posts)
        self.assertNotIn("correction", self.state()["requests"])

    def test_malformed_attribution_is_repaired_once_then_verified_normally(self):
        original = copy.deepcopy(self.complete_implementation([self.BAD]))
        receipt_path = self.directory() / original["receipt"]
        receipt = receipt_path.read_bytes()
        self.assertIn("engineering_error", original)
        self.assertNotIn("engineering_record", original)

        posts = len(self.fake.posts)
        answer = self.send_correction()
        self.assertEqual(self.send_correction(), answer)
        self.assertEqual(len(self.fake.posts), posts + 1)
        self.assertEqual(self.fake.posts[-1][1]["text"], "Continue.")
        proof = self.state()["requests"]["correction"]["carried"]["report_correction_admission"]
        self.assertEqual(proof["kind"], "malformed_delegate_attribution")
        self.assertEqual(proof["rejected_attribution_ids"], [self.BAD])
        self.assertEqual(proof["verified_provider_jobs"], [])

        corrected = self.complete_correction([])
        self.assertIn("engineering_record", corrected, corrected.get("engineering_error"))
        record = ao.read(self.directory() / corrected["engineering_record"])
        self.assertEqual(record["delegation"], [])
        self.assertEqual(self.state()["requests"]["implementation"], original)
        self.assertEqual(receipt_path.read_bytes(), receipt)
        with self.assertRaisesRegex(ao.RoomError, "passed verification checkpoint"):
            self.send("acceptance_review")
        self.review()
        self.assertTrue(self.service.ao_room_accept(self.room, "acceptance_review")["accepted"])

    def test_corrected_report_naming_malformed_id_is_not_captured(self):
        self.complete_implementation([self.BAD])
        self.send_correction()
        corrected = self.complete_correction([self.BAD])
        self.assertNotIn("engineering_record", corrected)
        self.assertIn("engineering_error", corrected)

    def test_provider_shaped_id_only_is_refused(self):
        self.assert_provider_shaped_refusal([self.PROVIDER_ID])

    def test_malformed_then_provider_shaped_id_is_refused(self):
        self.assert_provider_shaped_refusal([self.BAD, self.PROVIDER_ID])

    def test_provider_shaped_then_malformed_id_is_refused(self):
        self.assert_provider_shaped_refusal([self.PROVIDER_ID, self.BAD])

    def test_changed_candidate_refuses_report_only_correction(self):
        self.complete_implementation([self.BAD])
        (self.repo / "feature.txt").write_text("changed after completion\n")
        before = (self.directory() / "state.json").read_bytes()
        posts = copy.deepcopy(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, "candidate changed since original completion"):
            self.send_correction()
        self.assertEqual((self.directory() / "state.json").read_bytes(), before)
        self.assertEqual(self.fake.posts, posts)


class CorrectionIdentityTests(unittest.TestCase):
    def test_invalid_request_identities_fail_before_receipt_read(self):
        for identity in (["x"], {"a": 1}, True, None, "", "  "):
            with self.subTest(identity=identity):
                state = {"requests": {}}
                request = {"request_id": identity}
                with patch.object(ao_workflow, "completed_receipt") as receipt:
                    with self.assertRaisesRegex(ao.RoomError, "Invalid report-only correction evidence"):
                        ao_workflow.validate_report_correction(None, None, state, request)
                receipt.assert_not_called()

    def test_request_identity_must_match_its_saved_key_before_receipt_read(self):
        request = {"request_id": "B"}
        state = {"requests": {"A": request}}
        with patch.object(ao_workflow, "completed_receipt") as receipt:
            with self.assertRaisesRegex(ao.RoomError, "Invalid report-only correction evidence"):
                ao_workflow.validate_report_correction(None, None, state, request)
        receipt.assert_not_called()

    def test_predecessor_identity_must_match_its_saved_key_before_next_receipt(self):
        proof = {"request_id": "P"}
        request = {"request_id": "A", "purpose": "correction", "created_order": 2,
                   "carried": {"report_correction_admission": proof}}
        prior = {"request_id": "Q", "purpose": "implementation", "created_order": 1}
        state = {"requests": {"A": request, "P": prior}}
        receipt_value = {"carried_sha256": ao.digest(request["carried"])}
        with (
            patch.object(ao_workflow, "completed_receipt", return_value=receipt_value) as receipt,
            patch.object(ao_workflow, "engineering_report", return_value={}),
            patch.object(ao_delegates, "assert_settled"),
            patch.object(ao_delegates, "verify_delegation",
                         side_effect=ao_delegates.MalformedDelegateAttributionError(["bad"], [])),
            patch.object(ao_workflow, "_report_correction_proof", return_value=proof),
        ):
            with self.assertRaisesRegex(ao.RoomError, "Invalid report-only correction evidence"):
                ao_workflow.validate_report_correction(None, None, state, request)
        receipt.assert_called_once_with(None, request)

    def test_cycle_with_valid_saved_identities_is_reported_as_cyclic(self):
        request_a = {"request_id": "A", "purpose": "correction", "created_order": 2,
                     "carried": {"report_correction_admission": {"request_id": "B"}}}
        request_b = {"request_id": "B", "purpose": "correction", "created_order": 1,
                     "carried": {"report_correction_admission": {"request_id": "A"}}}
        state = {"requests": {"A": request_a, "B": request_b}}

        def receipt_value(_directory, request):
            return {"carried_sha256": ao.digest(request["carried"])}

        def matching_proof(_home, _directory, _state, request, _error):
            return {"request_id": request["request_id"]}

        with (
            patch.object(ao_workflow, "completed_receipt", side_effect=receipt_value) as receipt,
            patch.object(ao_workflow, "engineering_report", return_value={}),
            patch.object(ao_delegates, "assert_settled"),
            patch.object(ao_delegates, "verify_delegation",
                         side_effect=ao_delegates.MalformedDelegateAttributionError(["bad"], [])),
            patch.object(ao_workflow, "_report_correction_proof", side_effect=matching_proof),
        ):
            with self.assertRaisesRegex(ao.RoomError, "Cyclic report-only correction evidence"):
                ao_workflow.validate_report_correction(None, None, state, request_a)
        self.assertEqual(receipt.call_count, 2)


if __name__ == "__main__":
    unittest.main()
