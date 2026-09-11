"""Fake-only recovery contracts; no native process, account, network or key access."""

import copy
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import ao_project_room as ao
import ao_reviewer_recovery as recovery
import project_room
import project_room_mcp
import test_ao_project_room as fixtures


class RecoveryAO(fixtures.FakeAO):
    def __init__(self, path):
        self.workspaces = {}
        self.conversation_reads = 0
        self.before_conversation = None
        super().__init__(path)

    def add(self, name, harness="codex"):
        super().add(name, harness)
        snapshot = self.snapshots[name]
        snapshot.update(harness=harness, mode="chat", controller="stopped" if name == "reviewer" else "ready",
                        activeBranchId=snapshot["conversationId"] + ":root", latestSequence=0,
                        nativeForkAvailableAfterSequence=0, hasMoreBefore=False, branchedFromEarlierMessage=False,
                        activities=[], branchMaterialization={"strategy": "native", "replayTruncated": False})

    def _read_conversation(self, name):
        self.conversation_reads += 1
        if self.before_conversation:
            self.before_conversation(name)
        return super().conversation(name)

    def conversation(self, name):
        return self._read_conversation(name)

    def request(self, method, path, payload=None):
        if method == "GET" and "/conversation?" in path:
            self.gets += 1
            return self._read_conversation(path.split("/")[2])
        if method == "GET" and path.startswith("/desktop/sessions/"):
            self.gets += 1
            name = path.split("/")[3]
            return {"sessionId": name, "workspacePath": str(self.workspaces[name])}
        return super().request(method, path, payload)


class ReviewerRecoveryTests(unittest.TestCase):
    commit = fixtures.AdapterTests.commit
    bind = fixtures.AdapterTests.bind
    state = fixtures.AdapterTests.state

    def setUp(self):
        fixtures.AdapterTests.setUp(self)
        self.fake = RecoveryAO(self.repo)
        self.service.client_factory = lambda base: self.fake
        self.fake.add("replacement")
        for name in ("reviewer", "replacement"):
            target = self.root / (name + "-workspace")
            subprocess.run(["git", "-C", str(self.repo), "worktree", "add", "--detach", str(target), "HEAD"],
                           check=True, capture_output=True)
            self.fake.workspaces[name] = target
        self.bind("reviewer")
        self.service.ao_room_verify(self.room, str(self.repo))
        self.directory = self.service.root / "rooms" / self.room

    def audit(self):
        return self.service.ao_room_reviewer_recovery_audit(self.room)

    def args(self, audit=None):
        return {"room_id": self.room, "audit_sha256": (audit or self.audit())["audit_sha256"],
                "replacement_session_id": "replacement", "diagnosis": "The unused native thread has no materialized rollout",
                "authorization": "User approved the audited unused-reviewer recovery", "request_id": "unused-reviewer-1"}

    def recover(self, args=None):
        return self.service.ao_room_reviewer_recover(**(args or self.args()))

    def assert_refused_without_state_change(self, operation):
        before = (self.directory / "state.json").read_bytes()
        with self.assertRaises(ao.RoomError):
            operation()
        self.assertEqual((self.directory / "state.json").read_bytes(), before)
        self.assertEqual(self.fake.posts, [])

    def test_recovery_preserves_room_then_normal_review_and_acceptance_work(self):
        before = self.state()
        args = self.args()
        self.assertEqual(self.state(), before)  # Audit creates evidence only.
        result = self.recover(args)
        after = self.state()
        self.assertEqual(result["original_binding"], before["bindings"]["reviewer"])
        self.assertEqual(after["bindings"]["reviewer"]["session_id"], "replacement")
        comparable = copy.deepcopy(after)
        comparable.pop("reviewer_recovery")
        comparable["bindings"]["reviewer"] = before["bindings"]["reviewer"]
        self.assertEqual(comparable, before)
        self.assertEqual(self.fake.posts, [])
        self.assertEqual(recovery.validate(self.service, after)["original"], before["bindings"]["reviewer"])
        self.service.ao_room_send(self.room, "reviewer", "Review the unchanged candidate", "accept-1", purpose="acceptance_review")
        request = self.state()["requests"]["accept-1"]
        self.fake.finish("replacement", json.dumps({**request["review"], "decision": "approved", "review": "Checked exact evidence"}))
        self.service.ao_room_sync(self.room)
        accepted = self.service.ao_room_accept(self.room, "accept-1")
        self.assertTrue(accepted["accepted"])
        self.assertEqual(accepted["reviewer_session"], "replacement")
        self.assertEqual(len(self.state()["requests"]), 1)
        reads = self.fake.gets, self.fake.conversation_reads
        self.assertEqual(self.recover(args), result)  # Historical result even after actual review.
        self.assertEqual((self.fake.gets, self.fake.conversation_reads), reads)
        self.assertEqual(len(self.fake.posts), 1)

    def test_every_prior_reviewer_request_refuses_without_clearing_it(self):
        original = self.state()
        for status in ("uncertain", "submitted", "queued", "running", "failed", "interrupted", "cancelled", "recovered", "completed"):
            for purpose in ("acceptance_review", "implementation", None):
                with self.subTest(state=status, purpose=purpose):
                    state = copy.deepcopy(original)
                    state["requests"]["prior"] = {"request_id": "prior", "role": "reviewer", "session_id": "reviewer",
                        "purpose": purpose, "state": status, "provider_turn_id": "observed", "model": "astra"}
                    ao.atomic(self.directory / "state.json", state)
                    self.assert_refused_without_state_change(self.audit)

    def test_used_session_cannot_hide_behind_another_owned_role(self):
        state = self.state()
        state["requests"]["prior"] = {"request_id": "prior", "role": "engineer", "session_id": "reviewer",
                                      "state": "completed", "provider_turn_id": "observed"}
        ao.atomic(self.directory / "state.json", state)
        self.assert_refused_without_state_change(self.audit)

    def test_positive_empty_history_requires_every_observation(self):
        original = copy.deepcopy(self.fake.snapshots["reviewer"])
        fields = ("controller", "mode", "harness", "latestSequence", "nativeForkAvailableAfterSequence",
                  "hasMoreBefore", "turns", "messages", "activities", "branchMaterialization",
                  "branchedFromEarlierMessage", "conversationId", "activeBranchId", "settings")
        for field in fields:
            with self.subTest(missing=field):
                self.fake.snapshots["reviewer"] = copy.deepcopy(original)
                self.fake.snapshots["reviewer"].pop(field)
                self.assert_refused_without_state_change(self.audit)

    def test_nonempty_truncated_recovered_or_contradictory_native_history_refuses(self):
        original = copy.deepcopy(self.fake.snapshots["reviewer"])
        changes = [{"controller": "ready"}, {"latestSequence": 1}, {"latestSequence": False},
                   {"nativeForkAvailableAfterSequence": 1}, {"hasMoreBefore": True}, {"history_truncated": True},
                   {"branchedFromEarlierMessage": True}, {"messages": [{"id": "old", "role": "assistant"}]},
                   {"activities": [{"kind": "tool"}]}, {"modelReroute": {"toModel": "other"}},
                   {"branchMaterialization": {"strategy": "approximate", "replayTruncated": False}},
                   {"branchMaterialization": {"strategy": "native", "replayTruncated": True}},
                   {"branchMaterialization": "missing"}, {"usage": {"totalTokens": 1}},
                   {"usage": {"contextUsed": -1}}, {"usage": {"inputTokens": True}}]
        changes += [{"turns": [{"id": "old", "state": status}]} for status in
                    ("completed", "recovered", "failed", "interrupted", "cancelled", "queued", "running", "unknown")]
        for change in changes:
            with self.subTest(change=change):
                self.fake.snapshots["reviewer"] = copy.deepcopy(original) | change
                self.assert_refused_without_state_change(self.audit)

    def test_production_client_cannot_mask_missing_raw_history_for_either_reviewer(self):
        args = self.args()
        original = copy.deepcopy(self.fake.snapshots)
        with patch.object(self.fake, "conversation", side_effect=lambda name: ao.Client.conversation(self.fake, name)):
            for name in ("reviewer", "replacement"):
                for missing in (("messages",), ("turns",), ("messages", "turns"), ("activities",)):
                    with self.subTest(reviewer=name, missing=missing):
                        self.fake.snapshots = copy.deepcopy(original)
                        for field in missing:
                            self.fake.snapshots[name].pop(field)
                        normalized = self.fake.conversation(name)
                        for field in set(missing) & {"messages", "turns"}:
                            self.assertEqual(normalized[field], [])  # Production normalization formerly admitted this.
                        if name == "reviewer":
                            self.assert_refused_without_state_change(self.audit)
                        self.assert_refused_without_state_change(lambda: self.recover(args))
                        self.assertFalse((self.directory / "reviewer-recovery/requests").exists())

    def test_malformed_raw_arrays_refuse_without_state_or_receipt_change(self):
        args = self.args()
        original = copy.deepcopy(self.fake.snapshots)
        with patch.object(self.fake, "conversation", side_effect=lambda name: ao.Client.conversation(self.fake, name)):
            for name in ("reviewer", "replacement"):
                for field in ("messages", "turns", "activities"):
                    for value in (None, {}, False, 0, ""):
                        with self.subTest(reviewer=name, field=field, value=value):
                            self.fake.snapshots = copy.deepcopy(original)
                            self.fake.snapshots[name][field] = value
                            self.assert_refused_without_state_change(lambda: self.recover(args))
                            self.assertFalse((self.directory / "reviewer-recovery/requests").exists())

    def test_production_client_recovery_retains_exact_raw_observations(self):
        with patch.object(self.fake, "conversation", side_effect=lambda name: ao.Client.conversation(self.fake, name)):
            args = self.args()
            result = self.recover(args)
        audit = recovery._saved_audit(self.directory, args["audit_sha256"])
        receipt = ao.read(self.directory / result["receipt"])
        self.assertEqual(audit["observed_snapshot"]["raw_conversation"], self.fake.snapshots["reviewer"])
        self.assertEqual(receipt["old_snapshot"]["raw_conversation"], self.fake.snapshots["reviewer"])
        self.assertEqual(receipt["replacement_snapshot"]["raw_conversation"], self.fake.snapshots["replacement"])
        self.assertEqual(self.fake.posts, [])

    def test_raw_identity_is_validated_by_the_normal_identity_contract(self):
        args = self.args()
        def change_identity(name):
            if name == "replacement":
                self.fake.snapshots[name]["sessionId"] = "wrong-session"
        self.fake.before_conversation = change_identity
        self.assert_refused_without_state_change(lambda: self.recover(args))
        self.assertFalse((self.directory / "reviewer-recovery/requests").exists())

    def test_terminal_activity_seen_by_final_quiet_check_refuses_before_receipt_or_binding_write(self):
        args = self.args()
        original = copy.deepcopy(self.fake.snapshots)
        original_request = self.fake.request
        original_quiet = self.service.quiet
        for terminal in ("completed", "recovered", "failed", "interrupted", "cancelled"):
            with self.subTest(terminal=terminal):
                self.fake.snapshots = copy.deepcopy(original)
                admission_checks, injected = 0, False
                in_final_admission = False
                def quiet(state):
                    nonlocal admission_checks, in_final_admission
                    admission_checks += 1
                    in_final_admission = admission_checks == 2
                    try:
                        return original_quiet(state)
                    finally:
                        in_final_admission = False
                def wire_request(method, path, payload=None):
                    nonlocal injected
                    if in_final_admission and method == "GET" and path == "/sessions/reviewer/conversation?limit=500":
                        injected = True
                        self.fake.snapshots["reviewer"].update(
                            turns=[{"id": "external", "providerTurnId": "external-native", "state": terminal}],
                            messages=[{"id": "external-message", "role": "user", "text": "external work", "sequence": 1}],
                            latestSequence=1, nativeForkAvailableAfterSequence=1, usage={"totalTokens": 10})
                    return original_request(method, path, payload)
                with patch.object(self.service, "quiet", side_effect=quiet), patch.object(self.fake, "request", side_effect=wire_request), patch.object(
                        self.fake, "conversation", side_effect=lambda name: ao.Client.conversation(self.fake, name)):
                    self.assert_refused_without_state_change(lambda: self.recover(args))
                self.assertEqual(admission_checks, 2)
                self.assertTrue(injected)
                self.assertTrue(self.fake.snapshots["reviewer"]["turns"])
                self.assertFalse((self.directory / "reviewer-recovery/requests").exists())

    def test_candidate_gate_spec_and_saved_state_drift_refuse_stale_audit(self):
        args = self.args()
        original = self.state()
        spec_path = self.directory / original["spec"]
        checkpoint = ao.read(self.directory / original["checkpoint"])
        log_path = self.directory / checkpoint["gates"][0]["log"]
        for target in (self.repo / "feature.txt", spec_path, log_path):
            with self.subTest(path=target.name):
                contents = target.read_bytes()
                try:
                    if target == spec_path:
                        value = json.loads(contents)
                        value["content"] += " changed"
                        ao.atomic(target, value)
                    else:
                        target.write_bytes(contents + b"\nchanged\n")
                    self.assert_refused_without_state_change(lambda: self.recover(args))
                finally:
                    target.write_bytes(contents)
        state = copy.deepcopy(original)
        state["authorization"] += " changed"
        ao.atomic(self.directory / "state.json", state)
        self.assert_refused_without_state_change(lambda: self.recover(args))

    def test_normal_room_requires_actual_engineering_ready_evidence(self):
        state = self.state()
        state["workflow"] = "fable_engineering"
        ao.atomic(self.directory / "state.json", state)
        self.assert_refused_without_state_change(self.audit)

    def test_acceptance_or_running_verification_never_recovers(self):
        original = self.state()
        for key, value in (("acceptances", [{"request_id": "prior"}]), ("verifications", [{"state": "running"}])):
            state = copy.deepcopy(original)
            state[key] = value
            ao.atomic(self.directory / "state.json", state)
            self.assert_refused_without_state_change(self.audit)

    def test_replacement_must_be_distinct_ready_empty_same_model_and_identity(self):
        args = self.args()
        self.assert_refused_without_state_change(lambda: self.recover(args | {"replacement_session_id": "reviewer"}))
        snapshot = copy.deepcopy(self.fake.snapshots["replacement"])
        for change in ({"controller": "stopped"}, {"settings": {"model": "other", "reasoningEffort": "max"}},
                       {"settings": {"model": "astra", "reasoningEffort": "high"}}, {"latestSequence": 2},
                       {"turns": [{"id": "old", "state": "completed"}]}, {"history_truncated": True}):
            self.fake.snapshots["replacement"] = copy.deepcopy(snapshot) | change
            self.assert_refused_without_state_change(lambda: self.recover(args))
        self.fake.snapshots["replacement"] = snapshot
        original = copy.deepcopy(self.fake.sessions["replacement"])
        for change in ({"projectId": "different"}, {"harness": "claude-code"}, {"kind": "orchestrator"}, {"mode": "tui"}):
            self.fake.sessions["replacement"] = original | change
            self.assert_refused_without_state_change(lambda: self.recover(args))

    def test_reviewer_workspaces_must_be_verified_and_separate(self):
        original = dict(self.fake.workspaces)
        for name in ("reviewer", "replacement"):
            for path in (self.repo, self.root / "missing", Path("relative")):
                self.fake.workspaces = original | {name: path}
                self.assert_refused_without_state_change(lambda: self.recover())
        self.fake.workspaces = original | {"replacement": original["reviewer"]}
        self.assert_refused_without_state_change(lambda: self.recover())

    def test_a_new_session_cannot_alias_a_claimed_native_conversation(self):
        args = self.args()
        original = self.fake.snapshots["reviewer"]
        self.fake.snapshots["replacement"].update(conversationId=original["conversationId"],
                                                 activeBranchId=original["activeBranchId"])
        self.assert_refused_without_state_change(lambda: self.recover(args))

    def test_retired_and_active_cross_room_claims_are_preserved(self):
        other = self.service.ao_room_open(str(self.repo), "other", "project", "Authorized fixture", "http://127.0.0.1:1234",
            workflow="astra_led", exception_authorization="Authorized fixture")["room_id"]
        self.service.ao_room_bind(other, "reviewer", "replacement", "astra", "max")
        self.assert_refused_without_state_change(lambda: self.recover())
        # A separate fixture room cannot release a claim by swapping its reviewer.
        with self.assertRaisesRegex(ao.RoomError, "immutable"):
            self.service.ao_room_bind(other, "reviewer", "reviewer", "astra", "max")

    def test_success_keeps_original_session_reserved_and_limits_recovery_to_one(self):
        args = self.args()
        self.recover(args)
        other = self.service.ao_room_open(str(self.repo), "other", "project", "Authorized fixture", "http://127.0.0.1:1234",
            workflow="astra_led", exception_authorization="Authorized fixture")["room_id"]
        for name in ("reviewer", "replacement"):
            with self.assertRaisesRegex(ao.RoomError, "bound or retired"):
                self.service.ao_room_bind(other, "reviewer", name, "astra", "max")
        self.assert_refused_without_state_change(self.audit)
        for changes in ({"request_id": "second"}, {"authorization": "changed"}, {"replacement_session_id": "other"}):
            self.assert_refused_without_state_change(lambda: self.recover(args | changes))

    def test_tampered_audit_and_committed_receipt_block_mutation_and_status(self):
        args = self.args()
        audit_path = recovery._audit_path(self.directory, args["audit_sha256"])
        data = audit_path.read_bytes()
        value = json.loads(data)
        value["evidence"]["state_sha256"] = "0" * 64
        ao.atomic(audit_path, value)
        self.assert_refused_without_state_change(lambda: self.recover(args))
        audit_path.write_bytes(data)
        result = self.recover(args)
        receipt = self.directory / result["receipt"]
        value = ao.read(receipt)
        value["original"]["model"] = "other"
        ao.atomic(receipt, value)
        self.assert_refused_without_state_change(lambda: self.recover(args))
        with self.assertRaises(ao.RoomError):
            self.service.ao_room_status(self.room)
        with self.assertRaises(ao.RoomError):
            self.service.ao_room_send(self.room, "reviewer", "Never dispatch", "blocked")
        self.assertEqual(self.fake.posts, [])

    def test_interrupted_commit_preserves_original_and_refuses_pending_receipt(self):
        args = self.args()
        before = self.state()
        with patch.object(self.service, "save", side_effect=OSError("simulated interruption")), self.assertRaises(OSError):
            self.recover(args)
        self.assertEqual(self.state(), before)
        self.assertTrue((self.directory / "reviewer-recovery/requests/unused-reviewer-1.json").is_file())
        self.assert_refused_without_state_change(lambda: self.recover(args))
        with self.assertRaisesRegex(ao.RoomError, "uncommitted"):
            self.service.ao_room_send(self.room, "reviewer", "Never dispatch", "blocked")
        self.assertEqual(self.fake.posts, [])

    def test_activity_appearing_during_final_checks_refuses_before_commit(self):
        args = self.args()
        target_reads = 0
        def change_on_second_target(name):
            nonlocal target_reads
            if name == "replacement":
                target_reads += 1
                if target_reads == 2:
                    self.fake.snapshots[name]["turns"] = [{"id": "external", "state": "running"}]
        self.fake.before_conversation = change_on_second_target
        self.assert_refused_without_state_change(lambda: self.recover(args))
        self.assertFalse((self.directory / "reviewer-recovery/requests").exists())

    def test_candidate_change_during_last_native_check_is_not_committed(self):
        args = self.args()
        target_reads = 0
        def change_on_second_target(name):
            nonlocal target_reads
            if name == "replacement":
                target_reads += 1
                if target_reads == 2:
                    (self.repo / "feature.txt").write_text("changed during native checks\n")
        self.fake.before_conversation = change_on_second_target
        self.assert_refused_without_state_change(lambda: self.recover(args))
        self.assertFalse((self.directory / "reviewer-recovery/requests").exists())

    def test_unreadable_native_history_and_unrelated_git_repository_refuse(self):
        with patch.object(self.fake, "conversation", side_effect=ao.RoomError("AO unavailable")):
            self.assert_refused_without_state_change(self.audit)
        other = self.root / "different-repository"
        subprocess.run(["git", "clone", "--no-hardlinks", str(self.repo), str(other)], check=True, capture_output=True)
        self.fake.workspaces["replacement"] = other
        self.assert_refused_without_state_change(lambda: self.recover())

    def test_mcp_catalog_and_dispatch_keep_recovery_separate_from_binding(self):
        service = project_room.Service(self.root / "state")
        with patch.object(ao, "Service", return_value=self.service):
            response = project_room_mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "ao_room_reviewer_recovery_audit", "arguments": {"room_id": self.room}}}, service)
            self.assertTrue(response["result"]["structuredContent"]["eligible"])
        listing = project_room_mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, service)
        tools = {item["name"]: item for item in listing["result"]["tools"]}
        for name in ("ao_room_reviewer_recovery_audit", "ao_room_reviewer_recover"):
            self.assertFalse(tools[name]["annotations"]["readOnlyHint"])
            self.assertFalse(tools[name]["annotations"]["openWorldHint"])
        self.assertIn("authorization", tools["ao_room_reviewer_recover"]["inputSchema"]["required"])
        self.assertEqual(self.fake.posts, [])


if __name__ == "__main__":
    unittest.main()
