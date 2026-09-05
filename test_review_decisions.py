"""A user product decision permits a bounded next round, never a delivery bypass."""

import json
import sqlite3
import unittest

import test_nondelivery
import test_room
import test_project_room_mcp


class ReviewDecisionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_room.RoomTests(methodName="test_restart_resumes_same_session_and_exact_model")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.fixture.mode.write_text("changes")
        self.decision = self.fixture.root / "user-decision.md"
        self.decision.write_bytes(b"Keep status filtering in scope. Preserve task order; put search in the backlog.\r\n")

    def decide(self, revision=1, expected=0):
        return self.fixture.call("decision", "--revision", str(revision), "--decision-file", str(self.decision), expected=expected)

    def exhaust(self, prefix="round1"):
        for index in range(3):
            self.fixture.ask(f"{prefix}-{index}")

    def turns(self):
        with sqlite3.connect(self.fixture.room / "room.sqlite3") as db:
            return list(db.execute("SELECT * FROM turns ORDER BY id"))

    def test_legacy_cap_requires_user_then_exactly_three_new_reviews(self):
        initial = self.fixture.call("status")
        self.assertEqual(initial["reviews_remaining"], 3)
        self.assertFalse(initial["user_decision_required"])
        self.exhaust()
        at_limit = self.fixture.call("status")
        self.assertTrue(at_limit["user_decision_required"])
        self.assertEqual(at_limit["review_budget"]["reviews_used"], 3)
        self.assertIn("3-turn review limit", self.fixture.ask("blocked-fourth", expected=2)["error"])
        before = self.turns()
        recorded = self.decide()
        self.assertEqual(recorded["status"], "completed")
        self.assertFalse(recorded["model_submitted"])
        self.assertFalse(recorded["implementation_authorized"])
        self.assertEqual(recorded["review_budget"]["review_round"], 2)
        self.assertEqual(recorded["review_budget"]["reviews_remaining"], 3)
        self.assertEqual(self.turns(), before)
        self.assertEqual(len(self.fixture.calls()), 3)
        self.assertFalse(self.fixture.call("status")["agreement"])
        self.exhaust("round2")
        final = self.fixture.call("status")
        self.assertEqual(final["review_budget"]["reviews_used"], 3)
        self.assertTrue(final["user_decision_required"])
        self.assertIn("3-turn review limit", self.fixture.ask("blocked-seventh", expected=2)["error"])
        self.assertEqual(len(self.fixture.calls()), 6)

    def test_decision_is_exactly_bound_audited_and_reaches_next_prompt(self):
        self.exhaust()
        recorded = self.decide()
        decision = recorded["user_decision"]
        self.assertEqual(decision["revision"], 1)
        self.assertEqual(decision["spec_sha256"], self.fixture.call("status")["spec_sha256"])
        self.assertEqual(decision["turn_high_watermark"], 3)
        self.assertEqual(decision["decision"].encode(), self.decision.read_bytes())
        with sqlite3.connect(self.fixture.room / "room.sqlite3") as db:
            content, watermark = db.execute("SELECT decision,turn_high_watermark FROM user_decisions").fetchone()
            self.assertEqual(content, self.decision.read_bytes())
            self.assertEqual(watermark, 3)
        next_turn = self.fixture.ask("chosen-direction-review")
        self.assertEqual(self.fixture.calls()[-1]["packet"]["user_decision"], decision)
        self.assertIn("--resume", self.fixture.calls()[-1]["argv"])
        self.assertEqual(next_turn["session_id"], self.fixture.calls()[0]["session"])
        transcript = self.fixture.root / "transcript.md"
        self.fixture.call("transcript", "--file", str(transcript))
        self.assertIn("User product decision", transcript.read_text())
        self.assertIn("Keep status filtering in scope.", transcript.read_text())

    def test_early_empty_and_stale_decisions_cannot_renew_budget(self):
        self.assertIn("only after", self.decide(expected=2)["error"])
        self.exhaust()
        self.decision.write_text("\n ")
        self.assertIn("nonempty", self.decide(expected=2)["error"])
        self.decision.write_text("Keep search in the backlog; preserve the original filtering scope.")
        self.fixture.spec.write_text("Revision two incorporates the user's selected direction.")
        self.fixture.call("spec", "--revision", "2", "--file", str(self.fixture.spec))
        self.assertIn("Stale revision", self.decide(expected=2)["error"])
        recorded = self.decide(revision=2)
        self.assertEqual(recorded["user_decision"]["revision"], 2)
        self.assertEqual(recorded["user_decision"]["spec_sha256"], self.fixture.call("status")["spec_sha256"])
        self.assertIn("only after", self.decide(revision=2, expected=2)["error"])

    def test_failed_uncertain_and_pending_delivery_cannot_be_bypassed(self):
        for state, mode in (("failed", "error"), ("uncertain", "malformed"), ("pending", "changes")):
            with self.subTest(state=state):
                self.fixture.room = self.fixture.root / ("room-" + state)
                self.fixture.call("init", "--config", str(self.fixture.config))
                self.fixture.call("spec", "--revision", "1", "--file", str(self.fixture.spec))
                self.fixture.mode.write_text("changes")
                self.fixture.ask("review-1")
                self.fixture.ask("review-2")
                self.fixture.mode.write_text(mode)
                self.fixture.ask("review-3", expected=0 if state == "pending" else 2)
                if state == "pending":
                    with sqlite3.connect(self.fixture.room / "room.sqlite3") as db:
                        db.execute("UPDATE turns SET status='pending' WHERE request_id='review-3'")
                before, count = self.turns(), len(self.fixture.calls())
                self.assertIn("cannot bypass", self.decide(expected=2)["error"])
                self.assertEqual(self.turns(), before)
                self.assertEqual(len(self.fixture.calls()), count)
                with sqlite3.connect(self.fixture.room / "room.sqlite3") as db:
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM user_decisions").fetchone()[0], 0)

    def test_cache_replays_do_not_spend_or_renew_rounds_and_history_stays(self):
        self.exhaust()
        first = self.decide()
        duplicate = self.fixture.ask("round1-0")
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(self.fixture.call("status")["reviews_remaining"], 3)
        self.assertEqual(len(self.fixture.calls()), 3)
        self.exhaust("round2")
        self.decision.write_text("Use the existing task labels and defer persisted filters; continue that chosen design.")
        second = self.decide()
        self.assertEqual(second["review_budget"]["review_round"], 3)
        self.assertEqual(first["user_decision"]["turn_high_watermark"], 3)
        self.assertEqual(second["user_decision"]["turn_high_watermark"], 6)
        with sqlite3.connect(self.fixture.room / "room.sqlite3") as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM turns").fetchone()[0], 6)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM user_decisions").fetchone()[0], 2)
        self.assertEqual(len(self.fixture.calls()), 6)

    def test_not_sent_neither_spends_budget_nor_blocks_user_decision(self):
        script = self.fixture.fake.read_text()
        self.fixture.fake.write_text(script.replace('if mode == "timeout":', test_nondelivery.AUTH_FIXTURE + '\nif mode == "timeout":', 1))
        self.fixture.mode.write_text("auth")
        self.fixture.ask("not-delivered", expected=2)
        note = self.fixture.root / "diagnosis.md"
        note.write_text("Exact zero-usage local authentication failure; fix the local namespace without replaying this request.")
        self.fixture.call("recover-not-sent", "--request-id", "not-delivered", "--note-file", str(note))
        self.assertEqual(self.fixture.call("status")["reviews_remaining"], 3)
        self.fixture.mode.write_text("changes")
        self.exhaust()
        checkpoint = self.decide()
        self.assertEqual(checkpoint["user_decision"]["turn_high_watermark"], 4)
        self.assertEqual(checkpoint["review_budget"]["reviews_remaining"], 3)
        self.assertEqual(len(self.fixture.calls()), 4)


class ReviewDecisionIntegrationTests(unittest.TestCase):
    def test_mcp_user_decision_opens_next_bounded_controller_review_round(self):
        fixture=test_project_room_mcp.ProjectRoomMcpTests(methodName="test_initialize_discovery_and_fake_auth_have_protocol_only_stdout")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.control(decision="changes_required")
        for index in range(3):
            job=fixture.tool("room_review_submit",{"room_id":fixture.room_id,"revision":1,"message":"Review this direction independently","request_id":f"bounded-review-{index}"})
            terminal=fixture.tool("room_job_status",{"job_id":job["id"],"wait_seconds":15})
            self.assertEqual(terminal["status"],"succeeded",terminal)
        limited=fixture.request("tools/call",{"name":"room_review_submit","arguments":{"room_id":fixture.room_id,"revision":1,"message":"Do not bypass the user decision","request_id":"blocked-fourth"}})
        self.assertTrue(limited["result"]["isError"])
        self.assertEqual(len(fixture.calls()),3)
        direction="Keep saved-filter names mandatory; defer sharing and use the current local storage format."
        decision=fixture.tool("room_decision_record",{"room_id":fixture.room_id,"revision":1,"decision":direction})
        self.assertEqual(decision["review_budget"]["reviews_remaining"],3)
        self.assertFalse(decision["model_submitted"])
        self.assertEqual(len(fixture.calls()),3)
        resumed=fixture.tool("room_review_submit",{"room_id":fixture.room_id,"revision":1,"message":"Review against the user's selected direction","request_id":"next-round-first"})
        terminal=fixture.tool("room_job_status",{"job_id":resumed["id"],"wait_seconds":15})
        self.assertEqual(terminal["status"],"succeeded",terminal)
        self.assertEqual(len(fixture.calls()),4)
        self.assertEqual(fixture.calls()[-1]["packet"]["user_decision"]["decision"],direction)
        self.assertEqual(fixture.calls()[-1]["session"],fixture.calls()[0]["session"])
        self.assertIn("--resume",fixture.calls()[-1]["argv"])
        status=fixture.tool("room_status",{"room_id":fixture.room_id})
        self.assertEqual(status["review"]["reviews_remaining"],2)


if __name__ == "__main__":
    unittest.main()
