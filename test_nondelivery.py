"""Narrow local-authentication non-delivery recovery; fake processes only."""

import json
from pathlib import Path
import sqlite3
import unittest

import test_room


AUTH_FIXTURE = r'''
if mode.startswith("auth"):
    usage = {"input_tokens":0,"output_tokens":0,"cache_read_input_tokens":0,"cache_creation_input_tokens":0}
    response = {"type":"result","subtype":"success","is_error":True,"terminal_reason":"api_error",
                "session_id":session,"modelUsage":{},"usage":usage,
                "result":"Not logged in · Please run /login","duration_api_ms":0,"total_cost_usd":0}
    edits = {
        "auth-wrong-session": ("session_id", "wrong-session"),
        "auth-wrong-message": ("result", "Not logged in · Please run /login "),
        "auth-duration": ("duration_api_ms", 1),
        "auth-cost": ("total_cost_usd", 0.01),
        "auth-bool-duration": ("duration_api_ms", False),
        "auth-bool-cost": ("total_cost_usd", False),
        "auth-model-usage": ("modelUsage", {"fable-exact-test":{"outputTokens":1}}),
        "auth-structured": ("structured_output", {"decision":"accept"}),
        "auth-terminal": ("terminal_reason", "interrupted"),
        "auth-not-error": ("is_error", False),
    }
    if mode in edits:
        key,value = edits[mode]
        response[key] = value
    for key in usage:
        if mode == "auth-" + key:
            usage[key] = 1
    if mode == "auth-missing-counter": del usage["input_tokens"]
    if mode == "auth-bool-counter": usage["input_tokens"] = False
    print(json.dumps(response), flush=True)
    if mode == "auth-timeout": time.sleep(30)
    sys.exit(2 if mode == "auth-exit-two" else 1)
'''


class NondeliveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_room.RoomTests(methodName="test_restart_resumes_same_session_and_exact_model")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root = self.fixture.root
        script = self.fixture.fake.read_text()
        self.fixture.fake.write_text(script.replace('if mode == "timeout":', AUTH_FIXTURE + '\nif mode == "timeout":', 1))
        self.note = self.root / "diagnosis.md"
        self.note.write_text("The saved exact local authentication failure has zero API duration, cost, and all four token counters. The local account namespace was corrected. No request is being replayed by this recovery.\n")

    def recover(self, expected=0, transcript=None):
        args=["recover-not-sent","--request-id","review-1","--note-file",str(self.note)]
        if transcript is not None:
            args += ["--session-transcript",str(transcript)]
        return self.fixture.call(*args,expected=expected)

    def row(self):
        with sqlite3.connect(self.fixture.room / "room.sqlite3") as db:
            db.row_factory = sqlite3.Row
            return dict(db.execute("SELECT * FROM turns WHERE request_id='review-1'").fetchone())

    def test_exact_recovery_preserves_evidence_and_allows_new_id_same_room(self):
        self.fixture.mode.write_text("auth")
        result = self.fixture.ask(expected=2)
        before = self.row()
        raw_before = Path(result["stdout_path"]).read_bytes()
        recovered = self.recover()
        self.assertEqual(recovered["status"],"not_sent")
        self.assertFalse(recovered["evidence"]["model_resubmitted"])
        self.assertEqual(len(self.fixture.calls()),1)
        after = self.row()
        self.assertEqual(after,{**before,"status":"not_sent"})
        self.assertEqual(Path(result["stdout_path"]).read_bytes(),raw_before)
        with sqlite3.connect(self.fixture.room / "room.sqlite3") as db:
            original,note = db.execute("SELECT original_turn_json,note FROM nondelivery_recoveries").fetchone()
            self.assertEqual(json.loads(original),before)
            self.assertEqual(note,self.note.read_bytes())
        status = self.fixture.call("status")
        self.assertEqual(status["blocking_turns"],[])
        self.assertEqual(status["session_id"],result["session_id"])
        self.assertFalse(status["agreement"])
        self.fixture.mode.write_text("accept")
        rejected = self.fixture.ask(expected=2)
        self.assertIn("will not be replayed",rejected["error"])
        next_turn = self.fixture.ask("review-after-diagnosis")
        self.assertEqual(next_turn["session_id"],result["session_id"])
        self.assertEqual(next_turn["status"],"completed")
        self.assertEqual(len(self.fixture.calls()),2)
        self.assertIn("--session-id",self.fixture.calls()[1]["argv"])
        self.assertNotIn("--resume",self.fixture.calls()[1]["argv"])
        transcript = self.root / "transcript.md"
        self.fixture.call("transcript","--file",str(transcript))
        self.assertIn("Non-delivery diagnosis",transcript.read_text())
        self.assertIn("Not logged in · Please run /login",transcript.read_text())

    def test_recovered_non_delivery_does_not_consume_review_cap(self):
        self.fixture.mode.write_text("auth")
        self.fixture.ask(expected=2)
        self.recover()
        self.fixture.mode.write_text("accept")
        for index in range(3):
            self.fixture.ask(f"actual-review-{index}")
        self.assertIn("3-turn review limit",self.fixture.ask("fourth-actual-review",expected=2)["error"])
        self.assertEqual(len(self.fixture.calls()),4)

    def test_nonzero_or_malformed_proof_wrong_identity_and_wrong_message_refused(self):
        modes = ["auth-" + key for key in ("input_tokens","output_tokens","cache_read_input_tokens","cache_creation_input_tokens")]
        modes += ["auth-wrong-session","auth-wrong-message","auth-duration","auth-cost","auth-bool-duration",
                  "auth-bool-cost","auth-model-usage","auth-structured","auth-terminal","auth-not-error",
                  "auth-missing-counter","auth-bool-counter","auth-exit-two"]
        for mode in modes:
            with self.subTest(mode=mode):
                self.fixture.room = self.root / mode
                self.fixture.call("init","--config",str(self.fixture.config))
                self.fixture.call("spec","--revision","1","--file",str(self.fixture.spec))
                self.fixture.mode.write_text(mode)
                self.fixture.ask(expected=2)
                before = self.row()
                count = len(self.fixture.calls())
                self.recover(expected=2)
                self.assertEqual(self.row(),before)
                self.assertEqual(len(self.fixture.calls()),count)
                with sqlite3.connect(self.fixture.room / "room.sqlite3") as db:
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM nondelivery_recoveries").fetchone()[0],0)

    def test_tampered_raw_timeout_and_missing_note_refused(self):
        self.fixture.mode.write_text("auth")
        response = self.fixture.ask(expected=2)
        stdout = Path(response["stdout_path"])
        original = stdout.read_bytes()
        stdout.write_bytes(original + b"\n")
        self.assertIn("modified",self.recover(expected=2)["error"])
        stdout.write_bytes(original)
        self.note.write_text("\n")
        self.assertIn("diagnosis note",self.recover(expected=2)["error"])
        self.note.write_text("Independent diagnosis of saved local failure")
        self.fixture.room = self.root / "timeout"
        self.fixture.call("init","--config",str(self.fixture.config))
        self.fixture.call("spec","--revision","1","--file",str(self.fixture.spec))
        self.fixture.mode.write_text("auth-timeout")
        timeout = self.fixture.ask(expected=2,timeout=1)
        self.assertEqual(timeout["status"],"uncertain")
        self.assertIn("exact failed local authentication",self.recover(expected=2)["error"])

    def test_recovery_is_not_repeatable_and_cannot_replace_room_session(self):
        self.fixture.mode.write_text("auth")
        self.fixture.ask(expected=2)
        with sqlite3.connect(self.fixture.room / "room.sqlite3") as db:
            original_session = db.execute("SELECT value FROM metadata WHERE key='session_id'").fetchone()[0]
            db.execute("UPDATE metadata SET value='different-session' WHERE key='session_id'")
        self.assertIn("does not prove",self.recover(expected=2)["error"])
        with sqlite3.connect(self.fixture.room / "room.sqlite3") as db:
            db.execute("UPDATE metadata SET value=? WHERE key='session_id'",(original_session,))
        self.recover()
        self.recover(expected=2)
        with sqlite3.connect(self.fixture.room / "room.sqlite3") as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM nondelivery_recoveries").fetchone()[0],1)
        self.assertEqual(len(self.fixture.calls()),1)

    def test_existing_local_error_session_is_audited_and_next_new_request_resumes_same_uuid(self):
        self.fixture.mode.write_text("auth")
        first=self.fixture.ask(expected=2)
        path=self.root / (first["session_id"]+".jsonl")
        event={"sessionId":first["session_id"],"cwd":str(self.fixture.room),"type":"assistant",
               "message":{"model":"<synthetic>","content":[{"type":"text","text":"Not logged in · Please run /login"}]}}
        path.write_text(json.dumps(event)+"\n")
        before=path.read_bytes()
        recovered=self.recover(transcript=path)
        self.assertTrue(recovered["evidence"]["resume_local_session"])
        self.assertFalse(recovered["evidence"]["model_resubmitted"])
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual(len(self.fixture.calls()),1)
        self.assertTrue(self.fixture.call("status")["session_started"])
        self.fixture.mode.write_text("accept")
        following=self.fixture.ask("new-request-after-local-auth-fix")
        self.assertEqual(following["session_id"],first["session_id"])
        self.assertIn("--resume",self.fixture.calls()[1]["argv"])
        self.assertNotIn("--session-id",self.fixture.calls()[1]["argv"])

    def test_local_session_evidence_mismatch_is_refused_without_state_changes(self):
        self.fixture.mode.write_text("auth")
        first=self.fixture.ask(expected=2)
        path=self.root / (first["session_id"]+".jsonl")
        before=self.row()
        for event in ({"sessionId":"wrong-session","cwd":str(self.fixture.room)},
                      {"sessionId":first["session_id"],"cwd":str(self.root / "another-room")},
                      {"sessionId":first["session_id"],"cwd":None}):
            path.write_text(json.dumps(event)+"\n")
            self.assertIn("metadata does not match",self.recover(expected=2,transcript=path)["error"])
            self.assertEqual(self.row(),before)
            self.assertFalse(self.fixture.call("status")["session_started"])
        wrong_path=self.root / "wrong-uuid.jsonl"
        wrong_path.write_text('{}\n')
        self.assertIn("exact saved UUID",self.recover(expected=2,transcript=wrong_path)["error"])
        path.write_text('not valid json\n')
        self.recover(expected=2,transcript=path)
        self.assertEqual(self.row(),before)

    def test_recovered_non_delivery_preserves_previous_same_spec_acceptance(self):
        self.fixture.mode.write_text("accept")
        self.fixture.ask("earlier-successful-review")
        self.fixture.approve()
        self.assertTrue(self.fixture.call("status")["agreement"])
        self.fixture.mode.write_text("auth")
        self.fixture.ask(expected=2)
        self.assertFalse(self.fixture.call("status")["agreement"])
        self.recover()
        restored=self.fixture.call("status")
        self.assertTrue(restored["agreement"])
        self.assertTrue(restored["fable_accepted"])
        self.assertEqual(len(self.fixture.calls()),2)


if __name__ == "__main__":
    unittest.main()
