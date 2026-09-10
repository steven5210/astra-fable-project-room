"""Offline contract tests; no AO process, account, socket, or model is used."""

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import ao_project_room as ao
import project_room
import project_room_mcp


class FakeAO:
    def __init__(self, path):
        self.path = path
        self.posts = []
        self.lose_ack = False
        self.sessions = {}
        self.snapshots = {}
        self.add("engineer")
        self.add("reviewer")

    def add(self, name, harness="codex"):
        self.sessions[name] = {"id": name, "projectId": "project", "harness": harness, "mode": "chat", "kind": "worker"}
        self.snapshots[name] = {"sessionId": name, "conversationId": name + "-native", "activeBranchId": "root",
                                "settings": {"model": "astra" if harness == "codex" else "fable", "reasoningEffort": "max"},
                                "turns": [], "messages": [], "usage": {}, "history_truncated": False}

    def conversation(self, name):
        return copy.deepcopy(self.snapshots[name])

    def request(self, method, path, payload=None):
        if path == "/projects/project":
            return {"project": {"id": "project", "path": str(self.path)}}
        name = path.split("/")[2]
        if method == "GET":
            return {"session": copy.deepcopy(self.sessions[name])}
        self.posts.append((path, payload))
        turn_id = "turn-" + str(len(self.posts))
        snapshot = self.snapshots[name]
        snapshot["turns"].append({"id": turn_id, "providerTurnId": "native-" + turn_id, "state": "running"})
        snapshot["messages"].append({"id": "user-" + turn_id, "role": "user", "text": payload["text"], "turnId": turn_id, "sequence": len(snapshot["messages"]) + 1})
        if self.lose_ack:
            raise ao.RoomError("Simulated lost acknowledgement")
        return {"turnId": turn_id, "state": "running", "duplicate": False}

    def finish(self, name, text="Done", usage=None, state="completed"):
        snapshot = self.snapshots[name]
        turn = snapshot["turns"][-1]
        turn["state"] = state
        snapshot["messages"].append({"id": "answer-" + turn["id"], "role": "assistant", "text": text, "turnId": turn["id"], "sequence": len(snapshot["messages"]) + 1})
        snapshot["usage"] = usage or {"inputTokens": 100, "cachedTokens": 80, "outputTokens": 10, "totalTokens": 110, "contextUsed": 75, "contextWindow": 1000}


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        for args in (("init",), ("config", "user.name", "Test"), ("config", "user.email", "test@example.invalid")):
            subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True)
        (self.repo / "feature.txt").write_text("verified behavior\n")
        self.commit()
        self.fake = FakeAO(self.repo)
        self.service = ao.Service(self.root / "state", lambda base: self.fake)
        self.room = self.service.ao_room_open(str(self.repo), "test", "project", "User authorized Astra implementation", "http://127.0.0.1:1234", workflow="astra_led", exception_authorization="User approved this task-scoped Astra exception")["room_id"]
        self.gates = [[sys.executable, "-c", "from pathlib import Path; assert Path('feature.txt').read_text() == 'verified behavior\\n'"]]
        self.service.ao_room_spec_put(self.room, 1, "Verify exact behavior and independent review.", self.gates, "Astra approves the authorized scope")

    def commit(self):
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "fixture"], check=True, capture_output=True)

    def bind(self, role="engineer", name=None, **kwargs):
        return self.service.ao_room_bind(self.room, role, name or role, kwargs.pop("model", "astra"), "max", **kwargs)

    def send(self, role="engineer", request_id="first"):
        return self.service.ao_room_send(self.room, role, "Do the scoped work", request_id)

    def state(self):
        return ao.read(self.service.root / "rooms" / self.room / "state.json")

    def verdict(self, decision="approved"):
        request = self.state()["requests"]["first"]
        return json.dumps({**request["review"], "decision": decision, "review": "Verified the intended behavior and exact gate evidence."})

    def review(self):
        self.bind("reviewer")
        self.service.ao_room_verify(self.room, str(self.repo))
        self.send("reviewer")
        self.fake.finish("reviewer", self.verdict())
        self.service.ao_room_sync(self.room)

    def test_exact_duplicate_is_read_only_even_after_spec_change(self):
        self.bind()
        first = self.send()
        self.assertEqual(self.send(), first)
        self.assertEqual(len(self.fake.posts), 1)
        with self.assertRaisesRegex(ao.RoomError, "another payload"):
            self.service.ao_room_send(self.room, "engineer", "Different", "first")
        self.fake.finish("engineer")
        self.service.ao_room_sync(self.room)
        self.service.ao_room_spec_put(self.room, 2, "Revised scope", self.gates, "Actual updated approval")
        self.assertEqual(self.send()["state"], "completed")
        self.assertEqual(len(self.fake.posts), 1)

    def test_lost_ack_reconciles_after_restart_without_replay(self):
        self.bind()
        self.fake.lose_ack = True
        self.assertEqual(self.send()["state"], "uncertain")
        self.service = ao.Service(self.root / "state", lambda base: self.fake)
        self.assertEqual(self.send()["state"], "uncertain")
        with self.assertRaisesRegex(ao.RoomError, "active or uncertain"):
            self.send(request_id="second")
        self.fake.finish("engineer")
        result = self.service.ao_room_sync(self.room)
        self.assertEqual(result["requests"][0]["state"], "completed")
        self.assertIn("delivery_error", result["requests"][0])
        self.assertEqual(len(self.fake.posts), 1)

    def test_absent_or_duplicate_message_never_proves_non_delivery(self):
        self.bind()
        self.fake.lose_ack = True
        self.send()
        message = self.fake.snapshots["engineer"]["messages"].pop()
        self.fake.finish("engineer")
        self.assertEqual(self.service.ao_room_sync(self.room)["requests"][0]["state"], "uncertain")
        self.fake.snapshots["engineer"]["messages"].extend([message, {**message, "id": "duplicate"}])
        self.assertEqual(self.service.ao_room_sync(self.room)["requests"][0]["state"], "uncertain")
        self.assertEqual(len(self.fake.posts), 1)

    def test_native_dispatch_ack_loss_keeps_original_failure_and_blocks_replay(self):
        self.bind()
        self.fake.lose_ack = True
        self.send()
        turn = self.fake.snapshots["engineer"]["turns"][-1]
        native_id = turn.pop("providerTurnId")
        self.fake.finish("engineer", "Dispatch acknowledgement lost", state="failed")
        turn["error"] = "Provider acknowledgement lost"
        result = self.service.ao_room_sync(self.room)
        self.assertEqual(result["requests"][0]["state"], "uncertain")
        state = self.state(); first_receipt = state["requests"]["first"]["receipt"]
        original = (self.service.root / "rooms" / self.room / first_receipt).read_bytes()
        with self.assertRaisesRegex(ao.RoomError, "active or uncertain"):
            self.send(request_id="second")
        self.service.ao_room_sync(self.room)
        self.assertEqual(len(self.state()["requests"]["first"]["receipt_history"]), 1)
        turn["providerTurnId"] = native_id
        self.fake.finish("engineer", "Native completion recovered")
        result = self.service.ao_room_sync(self.room)
        self.assertEqual(result["requests"][0]["state"], "completed")
        self.assertEqual(len(self.state()["requests"]["first"]["receipt_history"]), 2)
        self.assertEqual((self.service.root / "rooms" / self.room / first_receipt).read_bytes(), original)
        self.assertEqual(len(self.fake.posts), 1)

    def test_older_terminal_receipt_keeps_usage_when_native_identity_is_projected(self):
        self.bind(); self.send(); self.fake.finish("engineer")
        self.service.ao_room_sync(self.room)
        with self.service.locked(self.room) as (directory, state):
            state["requests"]["first"].pop("provider_turn_id")
            original = copy.deepcopy(state["requests"]["first"])
            self.service.save(directory, state)
        self.send(request_id="second")
        self.service.ao_room_sync(self.room)
        current = self.state()["requests"]["first"]
        self.assertEqual(current["usage"], original["usage"])
        self.assertEqual(current["receipt_sha256"], original["receipt_sha256"])
        self.assertEqual(current["provider_turn_id"], "native-turn-1")

    def test_older_failed_record_without_native_identity_cannot_authorize_new_work(self):
        self.bind(); self.send()
        self.fake.snapshots["engineer"]["turns"][-1].pop("providerTurnId")
        self.fake.finish("engineer", state="failed")
        self.service.ao_room_sync(self.room)
        with self.service.locked(self.room) as (directory, state):
            state["requests"]["first"]["state"] = "failed"  # Pre-hardening saved state.
            self.service.save(directory, state)
        with self.assertRaisesRegex(ao.RoomError, "lacks observed native delivery"):
            self.send(request_id="second")
        result = self.service.ao_room_sync(self.room)
        self.assertEqual(result["requests"][0]["state"], "uncertain")
        self.assertEqual(len(self.fake.posts), 1)

    def test_identity_model_effort_and_branch_changes_block(self):
        self.bind()
        self.fake.snapshots["engineer"]["settings"]["model"] = "another"
        with self.assertRaisesRegex(ao.RoomError, "model/effort"):
            self.send()
        self.fake.snapshots["engineer"]["settings"]["model"] = "astra"
        self.send()
        self.fake.snapshots["engineer"]["activeBranchId"] = "fork"
        with self.assertRaisesRegex(ao.RoomError, "branch changed"):
            self.service.ao_room_sync(self.room)
        self.assertEqual(len(self.fake.posts), 1)

    def test_bindings_are_distinct_immutable_and_cross_room_unique(self):
        self.bind()
        with self.assertRaisesRegex(ao.RoomError, "already bound"):
            self.bind("reviewer", "engineer")
        with self.assertRaisesRegex(ao.RoomError, "immutable"):
            self.bind("engineer", "reviewer")
        other = self.service.ao_room_open(str(self.repo), "other", "project", "Authorized", "http://127.0.0.1:1234", workflow="astra_led", exception_authorization="User approved this task-scoped Astra exception")["room_id"]
        with self.assertRaisesRegex(ao.RoomError, "already bound"):
            self.service.ao_room_bind(other, "reviewer", "engineer", "astra", "max")

    def test_claude_requires_reason_and_is_never_implicitly_used(self):
        self.fake.add("claude", "claude-code")
        with self.assertRaisesRegex(ao.RoomError, "actually needed"):
            self.bind("reviewer", "claude", model="fable")
        result = self.bind("reviewer", "claude", model="fable", fable_reason="User requested Claude-specific validation")
        self.assertEqual(result["harness"], "claude-code")
        self.assertEqual(self.fake.posts, [])

    def test_immutable_specs_include_gates_and_approval(self):
        with self.assertRaisesRegex(ao.RoomError, "immutable"):
            self.service.ao_room_spec_put(self.room, 1, "Changed", self.gates, "Approved")
        state = self.state()
        target = self.service.root / "rooms" / self.room / state["spec"]
        spec = ao.read(target)
        spec["gates"] = [["true"]]
        ao.atomic(target, spec)
        with self.assertRaisesRegex(ao.RoomError, "modified"):
            self.service.ao_room_verify(self.room, str(self.repo))

    def test_acceptance_requires_completed_exact_independent_response(self):
        self.review()
        result = self.service.ao_room_accept(self.room, "first")
        self.assertTrue(result["accepted"])
        self.assertEqual(result, self.service.ao_room_accept(self.room, "first"))
        self.assertEqual(len(self.state()["acceptances"]), 1)
        (self.repo / "feature.txt").write_text("regression")
        with self.assertRaisesRegex(ao.RoomError, "Candidate changed"):
            self.service.ao_room_accept(self.room, "first")

    def test_failed_gates_and_mutating_gates_cannot_be_reviewed(self):
        (self.repo / "feature.txt").write_text("wrong")
        result = self.service.ao_room_verify(self.room, str(self.repo))
        self.assertFalse(result["passed"])
        self.bind("reviewer")
        with self.assertRaisesRegex(ao.RoomError, "passed verification"):
            self.send("reviewer")
        self.service.ao_room_spec_put(self.room, 2, "Mutating test fixture", [[sys.executable, "-c", "from pathlib import Path; Path('feature.txt').write_text('changed')"]], "Approved test")
        result = self.service.ao_room_verify(self.room, str(self.repo))
        self.assertFalse(result["passed"])
        self.assertNotEqual(result["candidate_sha256"], result["after_sha256"])

    def test_later_gate_cannot_restore_and_hide_an_earlier_mutation(self):
        mutate = [sys.executable, "-c", "from pathlib import Path; p=Path('feature.txt'); p.write_text('changed'); assert p.read_text()=='changed'"]
        restore = [sys.executable, "-c", "from pathlib import Path; Path('feature.txt').write_text('verified behavior\\n')"]
        self.service.ao_room_spec_put(self.room, 2, "Test observable mutation at each gate boundary", [mutate, restore], "Approved")
        result = self.service.ao_room_verify(self.room, str(self.repo))
        self.assertFalse(result["passed"])
        self.assertEqual(len(result["gates"]), 1)
        self.assertEqual((self.repo / "feature.txt").read_text(), "changed")
        self.assertNotEqual(result["gates"][0]["candidate_sha256_after"], result["candidate_sha256"])

    def test_timeout_receipt_preserves_failure(self):
        self.service.ao_room_spec_put(self.room, 2, "Timeout fixture", [[sys.executable, "-c", "import time; time.sleep(10)"]], "Approved test")
        result = self.service.ao_room_verify(self.room, str(self.repo), 1)
        self.assertFalse(result["passed"])
        self.assertTrue(result["gates"][0]["timed_out"])
        self.assertEqual(self.service.ao_room_status(self.room)["latest_verification"]["state"], "failed")

    def test_tampered_logs_or_receipts_block_acceptance(self):
        self.review()
        state = self.state()
        directory = self.service.root / "rooms" / self.room
        checkpoint = ao.read(directory / state["checkpoint"])
        log = directory / checkpoint["gates"][0]["log"]
        original = log.read_bytes()
        log.write_text("fabricated pass")
        with self.assertRaisesRegex(ao.RoomError, "evidence was modified"):
            self.service.ao_room_accept(self.room, "first")
        log.write_bytes(original)
        (directory / state["requests"]["first"]["receipt"]).write_text("{}")
        with self.assertRaisesRegex(ao.RoomError, "receipt was modified"):
            self.service.ao_room_accept(self.room, "first")

    def test_crash_orphan_receipt_cannot_bless_a_fabricated_verdict(self):
        self.bind("reviewer")
        self.service.ao_room_verify(self.room, str(self.repo))
        self.send("reviewer")
        self.fake.finish("reviewer", self.verdict("rejected"))
        saved = self.state()
        directory = self.service.root / "rooms" / self.room
        with patch.object(self.service, "save", side_effect=OSError("simulated crash before registry save")):
            with self.assertRaises(OSError):
                self.service.ao_room_sync(self.room)
        self.assertEqual(self.state(), saved)
        path = next((directory / "receipts" / "first").glob("*.json"))
        fabricated = ao.read(path)
        fabricated["messages"][-1]["text"] = self.verdict("approved")
        ao.atomic(path, fabricated)
        with self.assertRaisesRegex(ao.RoomError, "observation was modified"):
            self.service.ao_room_sync(self.room)
        self.assertEqual(self.state(), saved)
        with self.assertRaisesRegex(ao.RoomError, "active or uncertain"):
            self.service.ao_room_accept(self.room, "first")

    def test_new_checkpoint_makes_previous_review_stale(self):
        self.review()
        self.service.ao_room_verify(self.room, str(self.repo))
        with self.assertRaisesRegex(ao.RoomError, "stale"):
            self.service.ao_room_accept(self.room, "first")

    def test_rejections_and_failed_turns_cannot_accept(self):
        self.bind("reviewer")
        self.service.ao_room_verify(self.room, str(self.repo))
        self.send("reviewer")
        self.fake.finish("reviewer", self.verdict("rejected"))
        self.service.ao_room_sync(self.room)
        with self.assertRaisesRegex(ao.RoomError, "rejected"):
            self.service.ao_room_accept(self.room, "first")

    def test_native_model_reroute_is_preserved_and_cannot_be_accepted(self):
        self.bind("reviewer")
        self.service.ao_room_verify(self.room, str(self.repo))
        self.send("reviewer")
        self.fake.finish("reviewer", self.verdict())
        self.fake.snapshots["reviewer"]["modelReroute"] = {"fromModel": "astra", "toModel": "substitute", "providerTurnId": "native-turn-1"}
        result = self.service.ao_room_sync(self.room)
        self.assertEqual(result["requests"][0]["configured_model"], "astra")
        self.assertEqual(result["requests"][0]["model_identity"], "contradicted")
        request = self.state()["requests"]["first"]
        receipt = ao.read(self.service.root / "rooms" / self.room / request["receipt"])
        self.assertEqual(receipt["modelReroute"]["toModel"], "substitute")
        with self.assertRaisesRegex(ao.RoomError, "substitution"):
            self.service.ao_room_accept(self.room, "first")

    def test_late_model_reroute_blocks_acceptance_and_preserves_receipt(self):
        self.review()
        request = self.state()["requests"]["first"]
        original = (self.service.root / "rooms" / self.room / request["receipt"]).read_bytes()
        self.fake.snapshots["reviewer"]["modelReroute"] = {"fromModel": "astra", "toModel": "substitute", "providerTurnId": "native-turn-1"}
        with self.assertRaisesRegex(ao.RoomError, "substitution"):
            self.service.ao_room_accept(self.room, "first")
        self.fake.snapshots["reviewer"].pop("modelReroute")
        with self.assertRaisesRegex(ao.RoomError, "substitution"):
            self.service.ao_room_accept(self.room, "first")
        self.assertEqual((self.service.root / "rooms" / self.room / request["receipt"]).read_bytes(), original)
        self.assertTrue(self.state()["requests"]["first"]["reroute_evidence"])

    def test_historical_reroute_for_another_turn_does_not_taint_this_review(self):
        self.review()
        self.fake.snapshots["reviewer"]["modelReroute"] = {"fromModel": "astra", "toModel": "substitute", "providerTurnId": "older-turn"}
        self.assertTrue(self.service.ao_room_accept(self.room, "first")["accepted"])

    def test_running_reroute_survives_later_return_to_pinned_model(self):
        self.bind("reviewer"); self.service.ao_room_verify(self.room, str(self.repo)); self.send("reviewer")
        reroute = {"fromModel": "astra", "toModel": "substitute", "providerTurnId": "native-turn-1"}
        self.fake.snapshots["reviewer"]["modelReroute"] = reroute
        running = self.service.ao_room_sync(self.room)["requests"][0]
        self.assertEqual((running["state"], running["model_identity"]), ("running", "contradicted"))
        self.assertEqual(ao.read(self.service.root / "rooms" / self.room / running["reroute_evidence"]), reroute)
        self.service.ao_room_sync(self.room)
        self.assertEqual(len(self.state()["requests"]["first"]["reroute_history"]), 1)
        self.fake.snapshots["reviewer"]["modelReroute"] = {"fromModel": "substitute", "toModel": "astra", "providerTurnId": "native-turn-1"}
        self.fake.finish("reviewer", self.verdict())
        self.service.ao_room_sync(self.room)
        with self.assertRaisesRegex(ao.RoomError, "substitution"):
            self.service.ao_room_accept(self.room, "first")
        self.assertEqual(self.state()["requests"]["first"]["model_reroute"], reroute)

    def test_running_reroute_survives_a_crash_before_evidence_file_write(self):
        self.bind("reviewer"); self.service.ao_room_verify(self.room, str(self.repo)); self.send("reviewer")
        reroute = {"fromModel": "astra", "toModel": "substitute", "providerTurnId": "native-turn-1"}
        self.fake.snapshots["reviewer"]["modelReroute"] = reroute
        write = ao.atomic
        def crash_at_evidence(path, value):
            if path.name.startswith("reroute-"):
                raise OSError("simulated crash before evidence write")
            return write(path, value)
        with patch.object(ao, "atomic", side_effect=crash_at_evidence), self.assertRaises(OSError):
            self.service.ao_room_sync(self.room)
        saved = self.state()["requests"]["first"]
        self.assertEqual(saved["model_reroute"], reroute)
        self.assertEqual(saved["state"], "submitted")
        path = self.service.root / "rooms" / self.room / saved["reroute_evidence"]
        self.assertFalse(path.exists())
        self.fake.snapshots["reviewer"].pop("modelReroute")
        self.fake.finish("reviewer", self.verdict())
        self.service.ao_room_sync(self.room)
        with self.assertRaisesRegex(ao.RoomError, "substitution"):
            self.service.ao_room_accept(self.room, "first")
        self.assertEqual(ao.read(path), reroute)

    def test_running_historical_reroute_is_not_assigned_to_current_turn(self):
        self.bind("reviewer"); self.service.ao_room_verify(self.room, str(self.repo)); self.send("reviewer")
        self.fake.snapshots["reviewer"]["modelReroute"] = {"fromModel": "astra", "toModel": "substitute", "providerTurnId": "older-turn"}
        self.service.ao_room_sync(self.room)
        self.assertNotIn("model_reroute", self.state()["requests"]["first"])
        self.fake.finish("reviewer", self.verdict()); self.service.ao_room_sync(self.room)
        self.assertTrue(self.service.ao_room_accept(self.room, "first")["accepted"])

    def test_review_attempts_are_bounded_across_spec_revisions(self):
        self.bind("reviewer")
        for index in range(3):
            if index:
                self.service.ao_room_spec_put(self.room, index + 1, f"Revision {index + 1}", self.gates, "Approved")
            self.service.ao_room_verify(self.room, str(self.repo))
            self.send("reviewer", f"request-{index}")
            self.fake.finish("reviewer", "Rejected")
            self.service.ao_room_sync(self.room)
        with self.assertRaisesRegex(ao.RoomError, "exhausted"):
            self.send("reviewer", "fourth")

    def test_codex_deltas_are_durable_and_not_double_counted(self):
        self.bind()
        self.send()
        self.fake.finish("engineer")
        self.service.ao_room_sync(self.room)
        self.send(request_id="second")
        self.fake.finish("engineer", usage={"inputTokens": 150, "cachedTokens": 110, "outputTokens": 15, "totalTokens": 165, "contextUsed": 92})
        result = self.service.ao_room_sync(self.room)
        self.assertEqual(result["requests"][1]["usage"]["totalTokens"], 55)
        self.assertEqual(result["usage"]["known_primary_subtotal"]["totalTokens"], 165)
        self.assertEqual(result, self.service.ao_room_sync(self.room))
        self.assertEqual(result["requests"][1]["usage"]["context_used"], 92)

    def test_claude_turn_snapshots_sum_instead_of_overwriting(self):
        self.fake.add("claude", "claude-code")
        self.bind("engineer", "claude", model="fable", fable_reason="Synthetic Claude usage contract test")
        self.send()
        self.fake.finish("claude")
        self.service.ao_room_sync(self.room)
        self.send(request_id="second")
        self.fake.finish("claude", usage={"inputTokens": 150, "cachedTokens": 110, "outputTokens": 15, "totalTokens": 165})
        result = self.service.ao_room_sync(self.room)
        self.assertEqual(result["usage"]["known_primary_subtotal"]["totalTokens"], 275)

    def test_unchanged_native_counters_are_unknown_for_both_harnesses(self):
        old = {"inputTokens": 100, "cachedTokens": 80, "outputTokens": 10, "totalTokens": 110}
        for harness in ("codex", "claude-code"):
            request = {"state": "completed", "turn_id": "new", "harness": harness,
                       "baseline": {"turn_ids": ["old"], "conversation_id": "conversation", "branch_id": "root", "usage": old}}
            snapshot = {"conversationId": "conversation", "activeBranchId": "root", "usage": old,
                        "turns": [{"id": "old", "state": "completed"}, {"id": "new", "state": "completed"}]}
            with self.subTest(harness=harness):
                self.assertEqual(ao.usage_receipt(request, snapshot), {"known": False, "reason": "unchanged_native_counters"})

    def test_overlapping_external_turn_makes_usage_unknown_not_zero(self):
        self.bind()
        self.send()
        self.fake.finish("engineer")
        self.fake.snapshots["engineer"]["turns"].append({"id": "external", "state": "completed"})
        result = self.service.ao_room_sync(self.room)
        self.assertFalse(result["requests"][0]["usage"]["known"])
        self.assertEqual(result["usage"]["unknown_requests"], ["first"])

    def test_usage_missing_or_reset_counters_are_unknown(self):
        self.bind()
        self.send()
        self.fake.finish("engineer", usage={"contextUsed": 12})
        result = self.service.ao_room_sync(self.room)
        self.assertEqual(result["requests"][0]["usage"]["reason"], "missing_native_counters")

    def test_failed_turn_does_not_reuse_previous_usage(self):
        self.bind()
        self.send()
        self.fake.finish("engineer", state="failed")
        result = self.service.ao_room_sync(self.room)
        self.assertEqual(result["requests"][0]["usage"]["reason"], "non_completed_turn")
        self.assertEqual(result["requests"][0]["state"], "uncertain")
        self.assertEqual(result["requests"][0]["ao_turn_state"], "failed")
        with self.assertRaisesRegex(ao.RoomError, "active or uncertain"):
            self.send(request_id="second")

    def test_interrupted_native_turn_cannot_be_replayed_despite_an_assigned_id(self):
        self.bind(); self.send(); self.fake.finish("engineer", state="interrupted")
        result = self.service.ao_room_sync(self.room)
        self.assertEqual(result["requests"][0]["state"], "uncertain")
        self.assertEqual(result["requests"][0]["provider_turn_id"], "native-turn-1")
        with self.assertRaisesRegex(ao.RoomError, "active or uncertain"):
            self.send(request_id="second")

    def test_old_failed_native_record_is_not_settled_merely_by_its_turn_id(self):
        self.bind(); self.send(); self.fake.finish("engineer", state="failed")
        self.service.ao_room_sync(self.room)
        with self.service.locked(self.room) as (directory, state):
            state["requests"]["first"]["state"] = "failed"
            self.service.save(directory, state)
        with self.assertRaisesRegex(ao.RoomError, "does not prove the native run stopped"):
            self.send(request_id="second")
        result = self.service.ao_room_sync(self.room)
        self.assertEqual(result["requests"][0]["state"], "uncertain")

    def test_status_has_no_network_or_transcript_and_legacy_state_untouched(self):
        legacy = self.root / "state" / "rooms" / "legacy"
        legacy.mkdir(parents=True)
        (legacy / "state.json").write_text('{"paused": true}')
        self.bind()
        self.send()
        with patch.object(self.fake, "request", side_effect=AssertionError("network")), patch.object(self.fake, "conversation", side_effect=AssertionError("network")):
            status = self.service.ao_room_status(self.room)
        self.assertNotIn("text", status["requests"][0])
        self.assertNotIn("baseline", status["requests"][0])
        self.assertEqual((legacy / "state.json").read_text(), '{"paused": true}')

    def test_status_remains_available_during_gate_and_unfinished_verifier_blocks_mutations(self):
        self.bind()
        with self.service.locked(self.room) as (directory, state):
            state["verifications"].append({"id": "interrupted", "state": "running"})
            self.service.save(directory, state)
            # A status call must not try to acquire the gate's exclusive lock.
            with patch.object(self.service, "locked", side_effect=AssertionError("blocking status lock")):
                self.assertEqual(self.service.ao_room_status(self.room)["latest_verification"]["state"], "running")
        with self.assertRaisesRegex(ao.RoomError, "unfinished verification"):
            self.send()
        with self.assertRaisesRegex(ao.RoomError, "unfinished verification"):
            self.service.ao_room_verify(self.room, str(self.repo))
        self.assertFalse(self.fake.posts)

    def test_unknown_usage_projection_is_bounded_without_losing_count(self):
        with self.service.locked(self.room) as (directory, state):
            state["requests"] = {str(i): {"request_id": str(i), "state": "failed"} for i in range(50)}
            self.service.save(directory, state)
        result = self.service.ao_room_status(self.room)
        self.assertEqual(len(result["usage"]["unknown_requests"]), 20)
        self.assertEqual(result["usage"]["unknown_request_count"], 50)
        self.assertTrue(result["usage"]["unknown_requests_truncated"])

    def test_status_keeps_new_active_request_after_lexically_later_history(self):
        self.bind()
        for i in range(20):
            self.send(request_id=f"z-{i:02}")
            self.fake.finish("engineer", usage={"inputTokens": 100 * (i + 1), "cachedTokens": 80 * (i + 1), "outputTokens": 10 * (i + 1), "totalTokens": 110 * (i + 1)})
            self.service.ao_room_sync(self.room)
        with patch.object(ao.time, "time", return_value=1):
            self.send(request_id="a-new-active")
        status = self.service.ao_room_status(self.room)
        self.assertEqual(len(status["requests"]), 20)
        self.assertEqual(status["requests"][-1]["request_id"], "a-new-active")
        self.assertEqual(status["requests"][-1]["created_order"], 21)
        self.assertEqual(status["requests"][-1]["state"], "submitted")
        self.assertNotIn("z-00", [r["request_id"] for r in status["requests"]])

    def test_room_discovery_is_saved_metadata_only(self):
        with patch.object(self.fake, "request", side_effect=AssertionError("network")):
            result = self.service.ao_room_list(str(self.repo))
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["rooms"][0]["room_id"], self.room)
        self.assertNotIn("authorization", result["rooms"][0])

    def test_project_identity_requires_an_explicit_absolute_ao_path(self):
        for path in ("", ".", "repo"):
            self.fake.path = path
            with self.subTest(path=path), self.assertRaisesRegex(ao.RoomError, "path/identifier mismatch"):
                self.service.ao_room_open(str(self.repo), "new", "project", "Authorized", "http://127.0.0.1:1234")

    def test_mcp_dispatch_and_annotations(self):
        service = project_room.Service(self.root / "state")
        with patch.object(project_room.ao_project_room, "Service", return_value=self.service):
            result = project_room_mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "ao_room_status", "arguments": {"room_id": self.room}}}, service)
        self.assertFalse(result["result"]["isError"])
        self.assertEqual(result["result"]["structuredContent"]["room_id"], self.room)
        listing = project_room_mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, service)
        annotations = {t["name"]: t["annotations"] for t in listing["result"]["tools"]}
        self.assertTrue(annotations["ao_room_status"]["readOnlyHint"])
        self.assertFalse(annotations["ao_room_sync"]["readOnlyHint"])
        self.assertTrue(annotations["ao_room_send"]["openWorldHint"])


class ClientTests(unittest.TestCase):
    def test_only_explicit_loopback_without_proxy_redirects_or_credentials(self):
        for value in ("https://127.0.0.1:12", "http://localhost:12", "http://example.com:12", "http://127.0.0.1:12/private", "http://secret@127.0.0.1:12", "http://127.0.0.1", "http://127.0.0.1:12?secret=x"):
            with self.subTest(value=value), self.assertRaises(ao.RoomError):
                ao.Client(value)
        self.assertEqual(ao.Client("http://[::1]:1234").base, "http://[::1]:1234")
        with self.assertRaisesRegex(ao.RoomError, "redirects"):
            ao.NoRedirect().redirect_request(None, None, 302, "", {}, "http://elsewhere")

    def test_pagination_preserves_latest_turn_state_and_flags_truncation(self):
        client = ao.Client("http://127.0.0.1:1234")
        pages = [{"turns": [{"id": "one", "state": "completed"}], "messages": [{"id": "new", "sequence": 9}], "hasMoreBefore": True, "oldestSequence": 9},
                 {"turns": [{"id": "one", "state": "running"}], "messages": [{"id": "old", "sequence": 1}], "hasMoreBefore": False, "oldestSequence": 1}]
        with patch.object(client, "request", side_effect=pages) as request:
            result = client.conversation("session")
        self.assertEqual(result["turns"][0]["state"], "completed")
        self.assertEqual([m["id"] for m in result["messages"]], ["old", "new"])
        self.assertIn("beforeSequence=9", request.call_args.args[1])
        self.assertFalse(result["history_truncated"])


if __name__ == "__main__":
    unittest.main()
