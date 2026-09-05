"""Durable optional proposals and user-decision provenance; no external calls."""

import json
from pathlib import Path
import subprocess
import sys
import unittest

import project_room
import room
from test_project_room import ProjectFixture, ROOT


class EnhancementTests(ProjectFixture):
    def proposal(self, **updates):
        return self.service.room_backlog_add(self.room_id, updates.pop("content", "Share saved filters"),
                                             updates.pop("rationale", "An optional collaboration feature"), **updates)

    def events(self):
        with self.service.db() as db:
            return [dict(value) for value in db.execute("SELECT * FROM events WHERE room_id=? ORDER BY id", (self.room_id,))]

    def test_new_proposal_is_pending_and_visible_after_service_restart(self):
        created = self.proposal()
        self.assertRegex(created["proposal_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(created["user_decision"], "pending")
        self.assertIsNone(created["decision_rationale"])
        self.assertTrue(created["needs_issue"])
        self.assertTrue(created["needs_user_decision"])
        self.assertIsNone(created["previous_event_id"])
        fresh = project_room.Service(self.home)
        latest = fresh.room_status(self.room_id)["enhancements"]
        self.assertEqual(latest, fresh.room_history(self.room_id)["enhancements"])
        self.assertEqual(latest[0]["proposal_id"], created["proposal_id"])
        self.assertEqual(latest[0]["event_id"], created["event_id"])
        self.assertEqual(self.calls(), [])

    def test_issue_link_and_user_decision_append_auditable_updates(self):
        created = self.proposal()
        linked = self.proposal(proposal_id=created["proposal_id"], issue_url="https://github.com/example/project-room/issues/12")
        approved = self.proposal(proposal_id=created["proposal_id"], user_decision="approved",
                                 decision_rationale="User: Include sharing in the next agreed spec.")
        self.assertEqual(approved["proposal_id"], created["proposal_id"])
        self.assertEqual(linked["previous_event_id"], created["event_id"])
        self.assertEqual(approved["previous_event_id"], linked["event_id"])
        self.assertEqual(approved["issue_url"], linked["issue_url"])
        self.assertFalse(approved["needs_issue"])
        self.assertFalse(approved["needs_user_decision"])
        events = self.events()
        self.assertEqual([json.loads(event["content"])["user_decision"] for event in events], ["pending", "pending", "approved"])
        self.assertEqual(len(self.service.room_status(self.room_id)["enhancements"]), 1)
        self.assertEqual(self.service.room_history(self.room_id)["enhancements"][0]["decision_rationale"], approved["decision_rationale"])

    def test_metadata_update_preserves_decision_but_changed_proposal_requires_new_opinion(self):
        approved = self.proposal(user_decision="approved", decision_rationale="User explicitly approves this optional feature.")
        linked = self.proposal(proposal_id=approved["proposal_id"], issue_url="https://github.com/example/project/issues/1")
        self.assertEqual(linked["user_decision"], "approved")
        self.assertEqual(linked["decision_rationale"], approved["decision_rationale"])
        changed = self.proposal(proposal_id=approved["proposal_id"], content="Public sharing plus organization billing")
        self.assertEqual(changed["user_decision"], "pending")
        self.assertIsNone(changed["decision_rationale"])
        self.assertEqual(changed["issue_url"], linked["issue_url"])
        self.assertTrue(changed["needs_user_decision"])
        self.assertEqual(json.loads(self.events()[0]["content"])["user_decision"], "approved")

    def test_every_nonpending_decision_requires_explicit_nonempty_user_evidence(self):
        for decision in ("approved", "declined", "deferred"):
            for evidence in (None, "", " \n ", False):
                with self.subTest(decision=decision, evidence=evidence), self.assertRaises(room.RoomError):
                    self.proposal(user_decision=decision, decision_rationale=evidence)
        self.assertEqual(self.events(), [])
        proposal = self.proposal(rationale="Fable suggested this and an engineering issue was deferred.")
        self.assertEqual(proposal["user_decision"], "pending")
        for decision in ("approved", "declined", "deferred"):
            recorded = self.proposal(proposal_id=proposal["proposal_id"], user_decision=decision,
                                     decision_rationale="User decision: " + decision)
            self.assertEqual(recorded["user_decision"], decision)
        with self.assertRaisesRegex(room.RoomError, "requires an explicit"):
            self.proposal(proposal_id=proposal["proposal_id"], decision_rationale="Ambiguous evidence without a decision")

    def test_issue_url_accepts_only_exact_github_issue_urls_without_publication(self):
        invalid = ["http://github.com/example/repo/issues/1", "https://github.example/example/repo/issues/1",
                   "https://example.com/example/repo/issues/1", "https://github.com@example.com/repo/issues/1",
                   "https://user@github.com/example/repo/issues/1", "https://github.com/example/repo/pull/1",
                   "https://github.com/example/repo/issues/0", "https://github.com/example/repo/issues/-1",
                   "https://github.com/example/repo/issues/1?query=x", "https://github.com/example/repo/issues/1#comment",
                   "https://github.com/example/../issues/1", "https://github.com/example/repo%2Fevil/issues/1",
                   "https://github.com/example/repo/issues/1/", " https://github.com/example/repo/issues/1", True]
        for url in invalid:
            with self.subTest(url=url), self.assertRaises(room.RoomError):
                self.proposal(issue_url=url)
        self.assertEqual(self.events(), [])
        for url in ("https://github.com/example/repo/issues/1", "https://github.com/example-owner/project_room.v2/issues/9876"):
            self.assertEqual(self.proposal(issue_url=url)["issue_url"], url)
        self.assertEqual(self.calls(), [])

    def test_unknown_or_other_room_proposal_cannot_be_updated(self):
        created = self.proposal()
        other = self.service.room_open(str(self.project), "Other feature")
        with self.assertRaisesRegex(room.RoomError, "for this room"):
            self.service.room_backlog_add(other["id"], "Changed", "Wrong room", proposal_id=created["proposal_id"],
                                         user_decision="approved", decision_rationale="Must not cross room boundaries")
        for identifier in ("0" * 32, "../escape", True, []):
            with self.subTest(identifier=identifier), self.assertRaises(room.RoomError):
                self.proposal(proposal_id=identifier)
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.service.room_status(other["id"])["enhancements"], [])

    def test_legacy_backlog_does_not_infer_user_approval_and_pending_proposals_remain_visible(self):
        self.service._event(self.room_id, "backlog", {"content": "Legacy technical enhancement", "rationale": "Fable deferred it"})
        self.assertEqual(self.service.room_status(self.room_id)["enhancements"], [])
        created = self.proposal()
        with self.service.db() as db:
            db.executemany("INSERT INTO events(room_id,kind,content,created_at) VALUES(?,'backlog',?,?)",
                           [(self.room_id, room.canonical({"content": "Legacy item " + str(index), "rationale": "Technical backlog"}), room.now())
                            for index in range(205)])
        history = self.service.room_history(self.room_id)
        self.assertEqual(len(history["events"]), 200)
        self.assertNotIn(created["event_id"], [event["id"] for event in history["events"]])
        self.assertEqual(history["enhancements"][0]["proposal_id"], created["proposal_id"])
        self.assertTrue(history["enhancements"][0]["needs_user_decision"])

    def test_user_proposal_approval_does_not_change_agreed_spec_or_implementation_permission(self):
        before = self.service.room_status(self.room_id)
        self.proposal(user_decision="approved", decision_rationale="User: I want this considered in the next spec.")
        after = self.service.room_status(self.room_id)
        self.assertEqual(before["review"], after["review"])
        self.assertFalse(after["ready_for_handoff"])
        self.assertEqual(after["handoffs"], [])
        self.assertEqual(after["jobs"], [])

    def test_concurrent_cli_updates_form_one_serial_history_chain(self):
        created = self.proposal()
        arguments = {"room_id": self.room_id, "content": created["content"], "rationale": created["rationale"],
                     "proposal_id": created["proposal_id"]}
        processes = []
        for number in (1, 2):
            payload = {**arguments, "issue_url": f"https://github.com/example/project/issues/{number}"}
            processes.append(subprocess.Popen([sys.executable, str(ROOT / "project_room.py"), "--home", str(self.home),
                                               "call", "room_backlog_add", "--args", json.dumps(payload)],
                                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        for process in processes:
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stdout + stderr)
            self.assertEqual(json.loads(stdout)["proposal_id"], created["proposal_id"])
        events = self.events()
        self.assertEqual(len(events), 3)
        self.assertEqual([json.loads(event["content"])["previous_event_id"] for event in events],
                         [None, events[0]["id"], events[1]["id"]])
        self.assertEqual(self.service.room_status(self.room_id)["enhancements"][0]["event_id"], events[-1]["id"])

    def test_existing_tool_accepts_optional_metadata_without_expanding_tool_inventory(self):
        self.assertEqual(len(project_room.TOOL_SCHEMAS), 18)
        created = self.service.call("room_backlog_add", {"room_id": self.room_id, "content": "An optional improvement", "rationale": "Grounded benefit"})
        updated = self.service.call("room_backlog_add", {"room_id": self.room_id, "content": created["content"], "rationale": created["rationale"],
                                                         "proposal_id": created["proposal_id"], "user_decision": "deferred",
                                                         "decision_rationale": "User: Let's revisit this next month."})
        self.assertEqual(updated["user_decision"], "deferred")
        schema = project_room.TOOL_SCHEMAS["room_backlog_add"][1]
        self.assertEqual(set(schema["required"]), {"room_id", "content", "rationale"})
        with self.assertRaises(room.RoomError):
            self.proposal(user_decision="Fable approved")


if __name__ == "__main__":
    unittest.main()
