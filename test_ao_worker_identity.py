"""Synthetic W3 tests: real bounded source collection and real temporary SQL ownership.

Every room, owner database, parent/child transcript and receipt below is a real temporary file read
through the real bounded no-follow descriptor reader and the real ``ao_outcomes`` outcome/usable
consumer. Only these collaborators are supplied by the test and each patch is named explicitly:

* the reviewed W2 reader boundary (``ao_worker_identity.ao_model_boundaries.read``), whose own
  authentication has its own reviewed tests, so W3's consumer contract with it is what is bound here;
* ``ao_delegates.validate_preparation`` (this fixture has no real prepared AO delegate);
* ``ao_history_reconciliation.reconcile`` (unrelated to worker identity).

No model, provider, network, AO process or private transcript is used, and nothing here claims an
execution result.
"""

import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

import ao_evidence_audit
import ao_evidence_audit_io
import ao_evidence_audit_native
import ao_outcomes
import ao_worker_identity
from room import RoomError


REQUEST_TEXT = "Perform the exact authorized purpose."
NATIVE = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
ENGINEER_SESSION = "ao-session-w3"
ROOT_MODEL = "claude-fable-5-1"
EXPECTED = {"pr-sonnet": "claude-sonnet-5-5", "pr-opus": "claude-opus-5-5"}
CREATED_AT = 1767225610.0
COUNT_KEYS = ("read_requests", "evidence_root_requests", "outside_root_requests", "unclassified_paths",
              "reported_non_error_results", "reported_error_results", "unresolved_read_results",
              "duplicate_records", "duplicate_tool_observations", "repeated_identical_inputs", "distinct_slices",
              "unsupported_tool_observations")
TOP_KEYS = {"version", "coverage", "request_sha256", "owner_sha256", "sources", "parent", "children",
            "child_coverage", "reasons", "redundant_read_verdict", "limitations"}
LIMITATIONS = ["other_tool_access_not_counted", "lexical_paths_only", "reported_results_not_full_retrieval",
               "observed_counts_not_acceptance", "not_billing_or_quota", "bounded_time_correlation"]
LEGACY_OUTCOME_KEYS = {"version", "room_id", "request_id", "receipt_sha256", "text_sha256", "turn_id",
                       "provider_turn_id", "ao_terminal", "session_failures", "provider_failures", "native",
                       "outcome"}
API_ERROR_DETAIL = "synthetic quota (error type rate_limit, HTTP 429)"
API_ERROR_CONTENT = "Agent terminated early due to an API error: " + API_ERROR_DETAIL
API_ERROR_RESULT = "Error: " + API_ERROR_CONTENT


def canon(value):
    """Independent canonical encoding from the documented existing digest semantics."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                                     allow_nan=False).encode("utf-8")).hexdigest()


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def iso(seconds):
    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).isoformat()


class FakeService:
    """The only service surface the shared outcome path uses here: a real state save."""

    def __init__(self):
        self.saves = 0

    def save(self, directory, state):
        self.saves += 1
        (directory / "state.json").write_text(json.dumps(state, indent=2, sort_keys=True))


class Room:
    """One complete synthetic room: real files, real SQLite ownership, no mocked aggregates."""

    def __init__(self, base):
        self.base = Path(base).resolve()
        self.home = self.base / "home"
        self.room_id = "room-w3"
        self.directory = self.home / "ao" / "rooms" / self.room_id
        self.directory.mkdir(parents=True)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.config_root = self.base / "claude-config"
        self.project_dir = self.config_root / "projects" / str(self.workspace).replace("/", "-")
        self.subagents = self.project_dir / NATIVE / "subagents"
        self.subagents.mkdir(parents=True)
        self.transcript = self.project_dir / (NATIVE + ".jsonl")
        self.database = self.base / "ao.db"
        self.request_id = "req-w3-1"
        self.created_at = CREATED_AT
        self.observed_at = CREATED_AT + 60.0
        self.engineer_session = ENGINEER_SESSION
        self.model = ROOT_MODEL
        self.receipt = None
        self.write_owner()
        self.write_parent([])
        self.write_preparation()
        self.request = self.new_request()
        self.state = self.new_state()
        self.write_state()
        self.write_receipt()

    # ---- ownership, preparation, source --------------------------------------------------

    def write_owner(self, activity="idle", workspace=None):
        if self.database.exists():
            self.database.unlink()
        with sqlite3.connect(self.database) as db:
            db.executescript("""CREATE TABLE sessions(id,project_id,harness,session_mode,is_terminated,
              activity_state,workspace_path,provider_conversation_id,controller_generation);
              CREATE TABLE conversations(id,current_session_id,active_branch_id);
              CREATE TABLE conversation_branches(id,conversation_id,provider_conversation_id,session_id,
              strategy,replay_truncated);""")
            db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)",
                       (self.engineer_session, "project-1", "claude-code", "chat", 0, activity,
                        str(workspace or self.workspace), NATIVE, "generation-1"))
            db.execute("INSERT INTO conversations VALUES (?,?,?)",
                       ("conversation-1", self.engineer_session, "branch-1"))
            db.execute("INSERT INTO conversation_branches VALUES (?,?,?,?,?,?)",
                       ("branch-1", "conversation-1", NATIVE, self.engineer_session, "native", 0))

    def source(self):
        return {"database": str(self.database), "transcript": str(self.transcript),
                "session_id": self.engineer_session, "native_session_id": NATIVE}

    def prepared(self):
        return {"room_id": self.room_id, "worktree": str(self.workspace), "provider": "deepseek",
                "delegate_sha256": canon({"provider": "deepseek"}),
                "routing": {"version": 1, "files": {}, "guard_sha256": canon({"guard": 1}),
                            "claude_config_dir": str(self.config_root)}}

    def write_preparation(self):
        (self.directory / "preparation.json").write_text(json.dumps(self.prepared(), indent=2) + "\n")

    def binding(self):
        return {"role": "engineer", "session_id": self.engineer_session, "harness": "claude-code",
                "model": self.model, "reasoning_effort": "max", "conversation_id": "conversation-1",
                "branch_id": "branch-1"}

    # ---- request, state, receipt, snapshot ------------------------------------------------

    def new_request(self, **overrides):
        request = {"request_id": self.request_id, "role": "engineer", "session_id": self.engineer_session,
                   "harness": "claude-code", "model": self.model, "reasoning_effort": "max",
                   "conversation_id": "conversation-1", "branch_id": "branch-1", "purpose": "implementation",
                   "state": "completed", "turn_id": "turn-1", "provider_turn_id": "native-turn-1",
                   "observed_turn": {"id": "turn-1", "providerTurnId": "native-turn-1",
                                     "state": "completed"},
                   "created_at": self.created_at, "created_order": 1, "text": REQUEST_TEXT,
                   "text_sha256": sha256_text(REQUEST_TEXT),
                   "baseline": {"conversation_id": "conversation-1", "branch_id": "branch-1", "turn_ids": []}}
        request.update(overrides)
        return request

    def new_state(self, request=None, bindings=True):
        state = {"room_id": self.room_id, "ao_project_id": "project-1", "workflow": "fable_engineering",
                 "delegate": {"provider": "deepseek"}, "preparation": "preparation.json",
                 "preparation_sha256": canon(self.prepared()),
                 "requests": {self.request_id: request or self.request}, "native_outcome_source": self.source()}
        if bindings:
            state["bindings"] = {"engineer": self.binding()}
        return state

    def write_state(self):
        (self.directory / "state.json").write_text(json.dumps(self.state, indent=2, sort_keys=True))

    def receipt_payload(self, completed_seconds=30.0, messages=None):
        return {"turn": {"id": "turn-1", "providerTurnId": "native-turn-1", "state": "completed",
                         "completedAt": iso(self.created_at + completed_seconds), "stopReason": "end_turn"},
                "messages": messages if messages is not None else [
                    {"role": "user", "text": REQUEST_TEXT, "turnId": "turn-1"},
                    {"role": "assistant", "text": "Done.", "turnId": "turn-1"}],
                "settings": {"model": self.model, "reasoningEffort": "max"},
                "usage": {}, "modelReroute": None, "history_truncated": False}

    def write_receipt(self, request=None, payload=None):
        request = request if request is not None else self.request
        payload = payload or self.receipt_payload()
        relative = "receipts/%s/%s.json" % (request["request_id"], canon(payload))
        path = self.directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        receipt = {**payload, "observed_at": self.observed_at}
        path.write_text(json.dumps(receipt, indent=2) + "\n")
        request["receipt"] = relative
        request["receipt_sha256"] = canon(receipt)
        self.receipt = receipt
        return receipt

    def snapshot(self, payload=None, activities=None, failures=None):
        payload = payload or self.receipt_payload()
        return {"sessionId": self.engineer_session, "conversationId": "conversation-1",
                "activeBranchId": "branch-1", "settings": payload["settings"], "turns": [payload["turn"]],
                "messages": payload["messages"], "activities": list(activities or ()),
                "sessionFailures": list(failures or ()), "usage": {}, "history_truncated": False}

    # ---- transcript records ---------------------------------------------------------------

    def human(self, uuid="human-1", seconds=1.0, text=REQUEST_TEXT):
        return {"type": "user", "sessionId": NATIVE, "cwd": str(self.workspace), "uuid": uuid,
                "timestamp": iso(self.created_at + seconds), "isSidechain": False, "origin": {"kind": "human"},
                "message": {"role": "user", "content": text}}

    def launch(self, tool_id, uuid="launch-1", seconds=2.0, role="pr-opus", prompt="Do work", name="Agent",
               background=False, model=None, input_extra=None, **extra):
        inputs = {"subagent_type": role, "prompt": prompt, "run_in_background": background}
        inputs.update(extra)
        inputs.update(input_extra or {})
        return {"type": "assistant", "sessionId": NATIVE, "cwd": str(self.workspace), "uuid": uuid,
                "timestamp": iso(self.created_at + seconds), "isSidechain": False,
                "message": {"role": "assistant", "model": model or self.model,
                            "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": inputs}]}}

    def stop(self, uuid="parent-stop", seconds=2.7, model=None, stop_reason="end_turn"):
        return {"type": "assistant", "sessionId": NATIVE, "cwd": str(self.workspace), "uuid": uuid,
                "timestamp": iso(self.created_at + seconds), "isSidechain": False,
                "message": {"role": "assistant", "model": model or self.model,
                            "content": [{"type": "text", "text": "parent"}], "stop_reason": stop_reason}}

    def result(self, tool_id, uuid="result-1", seconds=3.0, launcher="launch-1", structured=None, content="done",
               is_error=None, source=None, extra_blocks=()):
        block = {"type": "tool_result", "tool_use_id": tool_id, "content": content}
        if is_error is not None:
            block["is_error"] = is_error
        row = {"type": "user", "sessionId": NATIVE, "cwd": str(self.workspace), "uuid": uuid,
               "timestamp": iso(self.created_at + seconds), "isSidechain": False,
               "sourceToolAssistantUUID": source if source is not None else launcher,
               "message": {"role": "user", "content": [block, *extra_blocks]}}
        if structured is not None:
            row["toolUseResult"] = structured
        return row

    def completed(self, agent_id, prompt="Do work", task_id=None):
        value = {"status": "completed", "agentId": agent_id, "prompt": prompt,
                "content": [{"type": "text", "text": "Finished."}], "totalToolUseCount": 1,
                "totalDurationMs": 1, "totalTokens": 0,
                "usage": {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": None,
                          "cache_read_input_tokens": None, "server_tool_use": None, "service_tier": None,
                          "cache_creation": None}}
        if task_id is not None:
            value["taskId"] = task_id
        return value

    def child(self, agent_id, uuid="c-1", seconds=2.5, launcher="launch-1", model="claude-opus-5-5",
              workspace=None, session=None, sidechain=True, stop_reason="end_turn", source=None,
              api_error=False, omit_source=False):
        message = {"role": "assistant", "content": [{"type": "text", "text": "worker done"}]}
        if model is not None:
            message["model"] = model
        if stop_reason is not None:
            message["stop_reason"] = stop_reason
        row = {"type": "assistant", "sessionId": session or NATIVE, "cwd": str(workspace or self.workspace),
               "uuid": uuid, "timestamp": iso(self.created_at + seconds), "isSidechain": sidechain,
               "agentId": agent_id,
               "message": message}
        if not omit_source:
            row["sourceToolAssistantUUID"] = source if source is not None else launcher
        if api_error:
            row["isApiErrorMessage"] = True
            row["error"] = "rate_limit"
        return row

    def child_local_result(self, agent_id, uuid="c-result-1", seconds=2.4, source="c-1",
                           tool_id="toolu_child_1"):
        """One child-local user tool-result row whose pointer names this child's own assistant row."""
        return {"type": "user", "sessionId": NATIVE, "cwd": str(self.workspace), "uuid": uuid,
                "timestamp": iso(self.created_at + seconds), "isSidechain": True, "agentId": agent_id,
                "sourceToolAssistantUUID": source,
                "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id,
                                                         "content": "child local result",
                                                         "is_error": False}]}}

    def write_parent(self, records):
        self.transcript.parent.mkdir(parents=True, exist_ok=True)
        self.transcript.write_text("".join(json.dumps(item) + "\n" for item in records))

    def write_child(self, agent_id, records):
        self.subagents.mkdir(parents=True, exist_ok=True)
        self.child_path(agent_id).write_text("".join(json.dumps(item) + "\n" for item in records))

    def child_path(self, agent_id):
        return self.subagents / ("agent-" + agent_id + ".jsonl")

    def parent_digest(self):
        return hashlib.sha256(self.transcript.read_bytes()).hexdigest()

    # ---- W3 consumers ---------------------------------------------------------------------

    def frozen_value(self, agents=None):
        return {"version": 1, "purpose": "implementation", "session_id": self.engineer_session,
                "routing_version": 3, "routing_sha256": "c" * 64, "authority": {"kind": "preparation"},
                "qualification_sha256": "b" * 64, "qualification_record": "qualifications/x.json",
                "preparation": "preparation.json", "preparation_sha256": canon(self.prepared()),
                "agents": dict(agents or EXPECTED), "worker_families": {"pr-sonnet": "sonnet", "pr-opus": "opus"},
                "agent_selection": {"pr-sonnet": {"kind": "family", "family": "sonnet"},
                                    "pr-opus": {"kind": "family", "family": "opus"}},
                "effort": "max", "basis": "synthetic frozen expectation for this test"}

    def frozen(self, agents=None):
        value = self.frozen_value(agents)
        return {"status": "frozen", "version": 1, "request_id": self.request_id,
                "session_id": value["session_id"], "purpose": value["purpose"], "expectation": value,
                "agents": value["agents"], "agent_selection": value["agent_selection"],
                "worker_families": value["worker_families"], "effort": value["effort"],
                "qualification_sha256": value["qualification_sha256"],
                "qualification_record": value["qualification_record"], "preparation": value["preparation"],
                "preparation_sha256": value["preparation_sha256"], "authority": value["authority"],
                "routing": {"sha256": value["routing_sha256"], "version": 3, "source": "preparation",
                            "routing_refresh": None},
                "basis": "synthetic frozen expectation for this test"}

    def workers(self, request=None, read=None, receipt=None, native=None):
        request = request if request is not None else self.request
        request.setdefault("native_worker_expectations", self.frozen_value())
        patched = {"side_effect": read} if isinstance(read, Exception) else {"return_value": self.frozen()}
        with mock.patch("ao_delegates.validate_preparation", return_value=self.prepared()), \
             mock.patch.object(ao_worker_identity.ao_model_boundaries, "read", **patched):
            return ao_worker_identity.observe_workers(
                self.directory, self.state, request,
                self.receipt if receipt is None else receipt,
                {"source_sha256": self.parent_digest()} if native is None else native)

    def observe_outcome(self, request=None, payload=None, failures=None, activities=None, read=None,
                        **observe_kwargs):
        request = request if request is not None else self.request
        payload = payload or self.receipt_payload()
        self.write_receipt(request, payload)
        snapshot = self.snapshot(payload, activities=activities, failures=failures)
        service = FakeService()
        reader = read if read is not None else self.frozen()
        with mock.patch("ao_delegates.validate_preparation", return_value=self.prepared()), \
             mock.patch.object(ao_worker_identity.ao_model_boundaries, "read", return_value=reader), \
             mock.patch("ao_history_reconciliation.reconcile", return_value=None):
            return ao_outcomes.observe(service, self.directory, self.state, request, snapshot,
                                       **observe_kwargs), service

    def qualified_parent(self, agent_id="agent-opus-1", role="pr-opus", tool_id="toolu_1", seconds=2.0,
                         result_seconds=3.0, prompt="Do work"):
        self.write_parent([self.human(), self.launch(tool_id, seconds=seconds, role=role, prompt=prompt),
                           self.result(tool_id, seconds=result_seconds,
                                       structured=self.completed(agent_id, prompt))])
        return agent_id

    def qualified_child(self, agent_id="agent-opus-1", model="claude-opus-5-5", **kwargs):
        self.write_child(agent_id, [self.child(agent_id, model=model, **kwargs)])
        return agent_id


class WorkerIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.room = Room(self.temporary.name)

    def qualified(self, **kwargs):
        agent_id = self.room.qualified_parent(**kwargs)
        return self.room.qualified_child(agent_id)

    # ---- exact identity, contradictions ---------------------------------------------------

    def test_exact_matching_child_identity_qualifies(self):
        agent_id = self.qualified()
        value = self.room.workers()
        self.assertEqual(value["status"], "qualified")
        self.assertTrue(value["qualified"])
        self.assertIsNone(value["reason"])
        self.assertEqual(value["coverage"], "complete")
        self.assertEqual(value["executed_roles"], ["pr-opus"])
        self.assertEqual(value["observed_models"], [EXPECTED["pr-opus"]])
        execution = value["executions"][0]
        self.assertEqual(execution["agent_id"], agent_id)
        self.assertEqual(execution["role"], "pr-opus")
        self.assertEqual(execution["source_sha256"],
                         hashlib.sha256(self.room.child_path(agent_id).read_bytes()).hexdigest())
        self.assertIsNotNone(execution["stop_row_uuid"])
        # configured family intent and MAX stay separate from the observed exact id
        self.assertEqual(value["expectation"]["effort"], "max")
        self.assertEqual(value["expectation"]["agent_selection"]["pr-opus"]["family"], "opus")
        self.assertEqual(value["parent"]["source_sha256"], self.room.parent_digest())

    def test_wrong_exact_version_of_the_same_family_is_a_contradiction(self):
        self.room.qualified_parent()
        self.room.qualified_child(model="claude-opus-5-1")
        value = self.room.workers()
        self.assertFalse(value["qualified"])
        self.assertEqual(value["status"], "unqualified")
        self.assertIn("child_model_mismatch", value["reasons"])
        self.assertEqual(value["launches"][0]["model"], "claude-opus-5-1")
        self.assertEqual(value["launches"][0]["expected_model"], EXPECTED["pr-opus"])
        self.assertEqual(value["executions"], [])

    def test_missing_child_model_row_is_recorded(self):
        self.room.qualified_parent()
        self.room.qualified_child(model=None)
        value = self.room.workers()
        self.assertIn("child_model_missing", value["reasons"])
        self.assertFalse(value["qualified"])
        self.assertIsNone(value["launches"][0]["model"])

    def test_multiple_conflicting_child_models_are_recorded(self):
        self.room.qualified_parent()
        self.room.write_child("agent-opus-1", [self.room.child("agent-opus-1", uuid="c-1", seconds=2.2,
                                                                model="claude-opus-5-5"),
                                                self.room.child("agent-opus-1", uuid="c-2", seconds=2.6,
                                                                model="claude-sonnet-5-5")])
        value = self.room.workers()
        self.assertIn("child_model_multiple", value["reasons"])
        self.assertEqual(value["launches"][0]["observed_models"], ["claude-opus-5-5", "claude-sonnet-5-5"])

    def test_duplicate_agreeing_child_stop_rows_still_qualify(self):
        self.room.qualified_parent()
        self.room.write_child("agent-opus-1", [self.room.child("agent-opus-1", uuid="c-1", seconds=2.2),
                                                self.room.child("agent-opus-1", uuid="c-2", seconds=2.6)])
        value = self.room.workers()
        self.assertTrue(value["qualified"])
        self.assertEqual(value["executions"][0]["stop_row_ids"], ["c-1", "c-2"])

    def test_a_child_row_outside_the_launch_interval_is_a_contradiction(self):
        self.room.qualified_parent()
        self.room.write_child("agent-opus-1", [self.room.child("agent-opus-1", uuid="c-1", seconds=2.2),
                                                self.room.child("agent-opus-1", uuid="c-2", seconds=9.0)])
        value = self.room.workers()
        self.assertIn("child_row_outside_interval", value["reasons"])
        self.assertFalse(value["qualified"])

    def test_parent_stop_row_is_not_a_child_identity(self):
        self.room.write_parent([self.room.human(), self.room.launch("toolu_1"),
                                self.room.result("toolu_1", structured=self.room.completed("agent-opus-1")),
                                self.room.stop(model=EXPECTED["pr-opus"])])
        self.room.qualified_child(model=None)
        value = self.room.workers()
        self.assertIn("child_model_missing", value["reasons"])
        self.assertEqual(value["observed_models"], [])

    def test_foreign_child_rows_and_workspace_mismatch_are_incomplete(self):
        self.room.qualified_parent()
        self.room.write_child("agent-opus-1", [self.room.child("agent-other", uuid="c-1")])
        value = self.room.workers()
        self.assertIn("child_attribution_ambiguous", value["reasons"])
        self.assertEqual(value["coverage"], "incomplete")
        self.room.write_child("agent-opus-1", [self.room.child("agent-opus-1", uuid="c-1",
                                                                workspace=self.room.base / "elsewhere")])
        value = self.room.workers()
        self.assertEqual(value["coverage"], "incomplete")
        self.assertIn("source_malformed", value["reasons"])

    def test_reused_structured_child_identity_across_fresh_launches(self):
        self.room.write_parent([self.room.human(),
                                self.room.launch("toolu_1", uuid="launch-1", seconds=2.0, prompt="One"),
                                self.room.result("toolu_1", uuid="result-1", seconds=2.5, launcher="launch-1",
                                                 structured=self.room.completed("agent-shared", "One")),
                                self.room.launch("toolu_2", uuid="launch-2", seconds=2.6, prompt="Two"),
                                self.room.result("toolu_2", uuid="result-2", seconds=2.9, launcher="launch-2",
                                                 structured=self.room.completed("agent-shared", "Two"))])
        value = self.room.workers()
        self.assertIn("child_identity_reused", value["reasons"])
        self.assertFalse(value["qualified"])
        self.assertEqual(len(value["launches"]), 2)
        self.assertEqual(value["executions"], [])
        self.assertEqual(value["observed_models"], [])
        self.assertEqual(value["executed_roles"], [])

    def test_explicit_error_result_agent_claim_reserves_the_identity(self):
        # A typed error result that explicitly names a valid structured agentId still reserves that
        # identity. A later launch whose own completion claims the same id is revoked by the
        # existing global reuse check; both launches stay visible and the earlier claim stays
        # unresolved rather than being dropped or silently attributed.
        self.room.write_parent([self.room.human(),
                                self.room.launch("toolu_1", uuid="launch-1", seconds=2.0, prompt="One"),
                                self.room.result("toolu_1", uuid="result-1", seconds=2.2, launcher="launch-1",
                                                 content="worker failed", is_error=True,
                                                 structured={"status": "failed", "agentId": "agent-shared",
                                                             "prompt": "One"}),
                                self.room.launch("toolu_2", uuid="launch-2", seconds=2.4, prompt="Two"),
                                self.room.result("toolu_2", uuid="result-2", seconds=2.6, launcher="launch-2",
                                                 structured=self.room.completed("agent-shared", "Two"))])
        self.room.write_child("agent-shared", [self.room.child("agent-shared", uuid="c-1", seconds=2.5,
                                                               omit_source=True)])
        value = self.room.workers()
        self.assertIn("child_identity_reused", value["reasons"])
        self.assertFalse(value["qualified"])
        self.assertEqual([entry["tool_use_id"] for entry in value["launches"]], ["toolu_1", "toolu_2"])
        self.assertEqual(value["launches"][1]["outcome"], "unsupported")
        self.assertIn("child_identity_reused", value["launches"][1]["reasons"])
        self.assertEqual(value["executions"], [])
        self.assertEqual(value["observed_models"], [])
        self.assertEqual(value["executed_roles"], [])

    def test_native_omitted_child_pointer_and_child_local_result_qualify(self):
        # The observed child schema: the first child assistant row omits sourceToolAssistantUUID and
        # a later child-local user tool-result row points at that same child assistant message.
        self.room.qualified_parent()
        agent_id = "agent-opus-1"
        self.room.write_child(agent_id, [
            self.room.child(agent_id, uuid="c-1", seconds=2.2, model=EXPECTED["pr-opus"],
                            omit_source=True),
            self.room.child_local_result(agent_id, uuid="c-2", seconds=2.3, source="c-1")])
        value = self.room.workers()
        self.assertTrue(value["qualified"])
        self.assertNotIn("child_attribution_ambiguous", value["reasons"])
        self.assertEqual(value["executions"][0]["agent_id"], agent_id)
        self.assertEqual(value["executions"][0]["model"], EXPECTED["pr-opus"])

    def test_launch_outside_the_exact_request_interval_is_not_attributed(self):
        self.room.write_parent([self.room.human(),
                                self.room.launch("toolu_9", uuid="launch-9", seconds=40.0),
                                self.room.result("toolu_9", uuid="result-9", seconds=41.0,
                                                 structured=self.room.completed("agent-late"))])
        value = self.room.workers()
        self.assertEqual(value["launches"], [])
        self.assertEqual(value["executions"], [])
        self.assertEqual(value["observed_models"], [])
        self.assertEqual(value["coverage"], "complete")

    def test_no_launch_is_zero_executions_not_an_observation_of_both_workers(self):
        self.room.write_parent([self.room.human()])
        value = self.room.workers()
        self.assertTrue(value["qualified"])
        self.assertEqual(value["executions"], [])
        self.assertEqual(value["executed_roles"], [])
        self.assertEqual(value["observed_models"], [])

    def test_unsupported_launch_shapes_are_reported_not_qualified(self):
        cases = (({"name": "Task"}, "launch_unsupported_task"),
                 ({"background": True}, "launch_unsupported_background"),
                 ({"resume": "previous"}, "launch_unsupported_launch"),
                 ({"input_extra": {"model": EXPECTED["pr-opus"]}}, "launch_unsupported_override"),
                 ({"role": "pr-haiku"}, "unknown_role"))
        for changes, reason in cases:
            with self.subTest(reason=reason):
                self.room.subagents.mkdir(parents=True, exist_ok=True)
                for path in self.room.subagents.glob("*.jsonl"):
                    path.unlink()
                self.room.write_parent([self.room.human(), self.room.launch("toolu_1", **changes),
                                        self.room.result("toolu_1",
                                                         structured=self.room.completed("agent-opus-1"))])
                value = self.room.workers()
                self.assertIn(reason, value["reasons"])
                self.assertFalse(value["qualified"])
                self.assertNotEqual(value["launches"][0]["outcome"], "qualified")

    def test_ignored_non_override_launch_keys_keep_the_child_evidence_path(self):
        # A launch input key the launch guard never sees is recorded, never a reason by itself.
        self.room.write_parent([self.room.human(),
                                self.room.launch("toolu_1", input_extra={"subject": "pr-opus"}),
                                self.room.result("toolu_1",
                                                 structured=self.room.completed("agent-opus-1"))])
        self.room.qualified_child("agent-opus-1")
        value = self.room.workers()
        self.assertTrue(value["qualified"])
        launch = value["launches"][0]
        self.assertEqual(launch["outcome"], "qualified")
        self.assertEqual(launch["extra_keys"], ["subject"])
        self.assertEqual(launch["extra_keys_ignored"], ["subject"])
        self.assertEqual(launch["reasons"], [])
        self.assertNotIn("launch_unsupported_override", value["reasons"])

    def test_override_keys_still_refuse_even_beside_ignored_ones(self):
        for override_key in ("model", "resume"):
            with self.subTest(override_key=override_key):
                for path in self.room.subagents.glob("*.jsonl"):
                    path.unlink()
                self.room.write_parent([self.room.human(),
                                        self.room.launch("toolu_1",
                                                         input_extra={"subject": "pr-opus",
                                                                      override_key: "steered"}),
                                        self.room.result("toolu_1",
                                                         structured=self.room.completed("agent-opus-1"))])
                self.room.qualified_child("agent-opus-1")
                value = self.room.workers()
                launch = value["launches"][0]
                self.assertIn("launch_unsupported_override", launch["reasons"])
                self.assertEqual(launch["extra_keys_ignored"], ["subject"])
                self.assertEqual(launch["outcome"], "unsupported")
                self.assertFalse(value["qualified"])

    def test_ignored_keys_still_require_the_child_evidence(self):
        self.room.write_parent([self.room.human(),
                                self.room.launch("toolu_1", input_extra={"subject": "pr-opus"}),
                                self.room.result("toolu_1",
                                                 structured=self.room.completed("agent-opus-1"))])
        value = self.room.workers()
        launch = value["launches"][0]
        self.assertEqual(launch["outcome"], "unresolved")
        self.assertEqual(launch["extra_keys_ignored"], ["subject"])
        self.assertEqual(launch["reasons"], ["child_missing"])
        self.assertIn("child_missing", value["reasons"])
        self.assertFalse(value["qualified"])
        self.assertEqual(value["status"], "incomplete")

    def test_a_held_unknown_outcome_clears_on_the_explicit_audit(self):
        request = self.room.request
        request.setdefault("native_worker_expectations", self.room.frozen_value())
        self.room.write_parent([self.room.human(),
                                self.room.launch("toolu_1", input_extra={"subject": "pr-opus"}),
                                self.room.result("toolu_1",
                                                 structured=self.room.completed("agent-opus-1"))])
        held, _ = self.room.observe_outcome(request)
        self.assertEqual(held["outcome"]["kind"], "unknown")
        self.assertTrue(held["outcome"]["hold"])
        with self.assertRaises(RoomError):
            ao_outcomes.usable(self.room.directory, request)
        # The child evidence appears; the explicit audit boundary re-observes and lifts the hold.
        self.room.qualified_child("agent-opus-1")
        cleared, _ = self.room.observe_outcome(request, allow_unknown_clear=True)
        self.assertEqual(cleared["outcome"]["kind"], "final_available")
        self.assertTrue(cleared["worker_observations"]["qualified"])
        self.assertEqual(cleared["worker_observations"]["launches"][0]["extra_keys_ignored"], ["subject"])
        ao_outcomes.usable(self.room.directory, request)

    def test_missing_and_torn_child_sources_are_incomplete(self):
        self.room.qualified_parent()
        value = self.room.workers()
        self.assertIn("child_missing", value["reasons"])
        self.assertEqual(value["status"], "incomplete")
        self.assertEqual(value["launches"][0]["outcome"], "unresolved")
        self.room.subagents.mkdir(parents=True, exist_ok=True)
        self.room.child_path("agent-opus-1").write_text(json.dumps(self.room.child("agent-opus-1")))
        value = self.room.workers()
        self.assertIn("source_torn", value["reasons"])
        self.assertEqual(value["coverage"], "incomplete")

    def test_source_changed_between_the_two_complete_passes(self):
        self.qualified()
        target = self.room.child_path("agent-opus-1")
        real_open = ao_evidence_audit_io._open_component
        calls = {"count": 0}

        def racing(name, flags, dir_fd):
            descriptor = real_open(name, flags, dir_fd)
            if name == "agent-agent-opus-1.jsonl":
                calls["count"] += 1
                if calls["count"] == 2:
                    target.write_text(target.read_text() + "\n")
            return descriptor

        with mock.patch("ao_evidence_audit_io._open_component", side_effect=racing):
            value = self.room.workers()
        self.assertIn("source_changed", value["reasons"])
        self.assertEqual(value["coverage"], "incomplete")
        self.assertFalse(value["qualified"])

    def test_active_owner_is_incomplete_coverage(self):
        self.qualified()
        self.room.write_owner(activity="working")
        value = self.room.workers()
        self.assertIn("source_active", value["reasons"])
        self.assertEqual(value["coverage"], "incomplete")

    def test_typed_terminal_child_api_failure_is_termination_not_a_served_model(self):
        self.room.write_parent([self.room.human(), self.room.launch("toolu_1"),
                                self.room.result("toolu_1", content=API_ERROR_CONTENT, is_error=True,
                                                 structured=API_ERROR_RESULT)])
        value = self.room.workers()
        self.assertEqual(value["launches"][0]["outcome"], "terminated_unqualified")
        self.assertIn("child_api_error_termination", value["reasons"])
        self.assertIsNone(value["launches"][0]["model"])
        self.assertEqual(value["executions"], [])
        self.assertFalse(value["qualified"])
        self.assertEqual(value["coverage"], "complete")

    # ---- provenance before the legacy branch -----------------------------------------------

    def test_absent_field_on_a_modern_request_still_authenticates_provenance(self):
        calls = []

        def reader(directory, state, retained):
            calls.append(retained["request_id"])
            return {"status": "absent", "request_id": retained["request_id"],
                    "reason": "no_frozen_worker_expectations"}

        modern = {**self.room.request, "prompt_projection": {"version": 1}}
        with mock.patch("ao_delegates.validate_preparation", return_value=self.room.prepared()), \
             mock.patch.object(ao_worker_identity.ao_model_boundaries, "read", side_effect=reader):
            self.assertIsNone(ao_worker_identity.observe_workers(self.room.directory, self.room.state, modern,
                                                                 self.room.receipt,
                                                                 {"source_sha256": self.room.parent_digest()}))
        self.assertEqual(calls, [self.room.request_id])
        legacy = self.room.new_request()
        with mock.patch.object(ao_worker_identity.ao_model_boundaries, "read", side_effect=reader):
            self.assertIsNone(ao_worker_identity.observe_workers(self.room.directory, self.room.state, legacy,
                                                                 None, None))
        self.assertEqual(calls, [self.room.request_id, self.room.request_id])

    def test_new_expectation_still_validates_after_a_later_routing_refresh(self):
        self.qualified()
        self.room.state["routing_refresh"] = {"path": "routing-refresh/later.json", "sha256": "d" * 64}
        value = self.room.workers()
        self.assertTrue(value["qualified"])
        self.assertEqual(value["expectation"]["routing"]["sha256"], "c" * 64)
        self.assertEqual(value["expectation"]["authority"], {"kind": "preparation"})

    def test_present_malformed_field_never_reads_as_absent(self):
        self.room.request["native_worker_expectations"] = {"version": 1}
        refusal = RoomError("Frozen worker expectations are present but empty, malformed or unsupported")
        value = self.room.workers(read=refusal)
        self.assertEqual(value["status"], "unqualified")
        self.assertEqual(value["reason"], "worker_expectation_malformed")
        self.assertFalse(value["qualified"])

    def test_legacy_request_keeps_the_old_outcome_shape(self):
        legacy = self.room.new_request()
        self.room.state["requests"] = {legacy["request_id"]: legacy}
        # Genuine historical native evidence for this retained request: its own caller packet and a
        # matching parent stop row, read through the real bounded collector. Only the W2 worker
        # expectation field is a genuine historical absence here.
        self.room.write_parent([self.room.human(), self.room.stop()])
        absent = {"status": "absent", "request_id": legacy["request_id"],
                  "reason": "no_frozen_worker_expectations"}
        value, service = self.room.observe_outcome(legacy, read=absent)
        self.assertNotIn("worker_observations", value)
        self.assertEqual(set(value), LEGACY_OUTCOME_KEYS)
        self.assertEqual(value["outcome"]["kind"], "final_available")
        ao_outcomes.usable(self.room.directory, legacy)
        self.assertEqual(service.saves, 1)

    # ---- shared outcome / acceptance gating ------------------------------------------------

    def test_worker_gap_withholds_final_availability_and_keeps_parent_evidence(self):
        self.room.qualified_parent()  # the child source is deliberately absent
        request = self.room.request
        request.setdefault("native_worker_expectations", self.room.frozen_value())
        value, service = self.room.observe_outcome(request)
        self.assertEqual(value["outcome"]["kind"], "unknown")
        self.assertTrue(value["outcome"]["hold"])
        self.assertFalse(value["worker_observations"]["qualified"])
        self.assertIn("child_missing", value["worker_observations"]["reasons"])
        self.assertNotIn("unknown", value["native"])
        self.assertEqual(value["native"]["source_sha256"], self.room.parent_digest())
        with self.assertRaises(RoomError):
            ao_outcomes.usable(self.room.directory, request)

    def test_matching_child_identity_reaches_final_availability_through_the_shared_path(self):
        self.qualified()
        request = self.room.request
        request.setdefault("native_worker_expectations", self.room.frozen_value())
        value, service = self.room.observe_outcome(request)
        self.assertTrue(value["worker_observations"]["qualified"])
        self.assertEqual(value["outcome"]["kind"], "final_available")
        ao_outcomes.usable(self.room.directory, request)
        self.assertEqual(service.saves, 1)

    def test_parent_quota_failure_keeps_precedence_over_an_interrupted_child(self):
        self.room.write_parent([self.room.human(), self.room.launch("toolu_1"),
                                self.room.result("toolu_1", content=API_ERROR_CONTENT, is_error=True,
                                                 structured=API_ERROR_RESULT)])
        request = self.room.request
        request.setdefault("native_worker_expectations", self.room.frozen_value())
        value, service = self.room.observe_outcome(request, failures=[{"turnId": "turn-1", "type": "rate_limit"}])
        self.assertEqual(value["outcome"]["kind"], "quota_limit")
        self.assertTrue(value["outcome"]["hold"])
        self.assertFalse(value["worker_observations"]["qualified"])
        self.assertIn("child_api_error_termination", value["worker_observations"]["reasons"])
        self.assertNotIn("unknown", value["native"])

    def test_first_sync_receipt_boundary_precedes_the_final_state_save(self):
        import ao_project_room
        self.qualified()
        request = self.room.request
        request.setdefault("native_worker_expectations", self.room.frozen_value())
        request["state"] = "completed"
        request.pop("receipt", None)
        request.pop("receipt_sha256", None)
        self.room.write_state()  # the on-disk state still lacks the just-persisted receipt
        payload = self.room.receipt_payload()
        # Exactly the controller's first-sync ordering: terminal state, persisted receipt through the
        # real Service.observation, then the shared outcome observation, and only then the save.
        self.assertTrue(ao_project_room.Service.observation(None, self.room.directory, request, payload))
        self.assertIn("receipt", request)
        value, service = self.room.observe_outcome(request, payload=payload)
        self.assertTrue(value["worker_observations"]["qualified"])
        self.assertEqual(value["outcome"]["kind"], "final_available")
        self.assertEqual(service.saves, 1)

    # ---- read-audit invariance ---------------------------------------------------------

    def test_identity_projection_is_opt_in_and_changes_no_default_observation(self):
        self.qualified()
        limits = {"bytes": ao_evidence_audit_io.MAX_TRANSCRIPT_BYTES,
                  "aggregate": ao_evidence_audit_io.MAX_AGGREGATE_BYTES,
                  "record": ao_evidence_audit_io.MAX_RECORD_BYTES, "records": ao_evidence_audit_io.MAX_RECORDS,
                  "record_ids": ao_evidence_audit_io.MAX_RECORD_IDS, "tool_ids": ao_evidence_audit_io.MAX_TOOL_IDS,
                  "depth": ao_evidence_audit_io.MAX_JSON_DEPTH,
                  "directory": ao_evidence_audit_io.MAX_DIRECTORY_ENTRIES}
        root = ao_evidence_audit_io.Root(str(self.room.config_root), "source_missing", "source_unsafe")
        self.addCleanup(root.close)
        parts = ("projects", self.room.project_dir.name, NATIVE + ".jsonl")
        scans = []
        for identity in (False, True):
            collector = ao_evidence_audit_native.Collector(limits, set())
            scan = ao_evidence_audit_native.RecordScanner(collector, "a" * 64, "parent", None, NATIVE,
                                                          str(self.room.workspace), str(self.room.workspace),
                                                          identity=identity)
            opened = root.open_file(parts, limits["bytes"], "source_missing", "source_unsafe",
                                    "transcript_bytes_limit")
            ao_evidence_audit_io.scan_source(opened, scan.record, collector, limits)
            scans.append(scan)
        interval, reasons = ao_evidence_audit_native.parent_interval(scans[0], self.room.request, self.room.receipt)
        self.assertEqual(reasons, ())
        self.assertEqual(ao_evidence_audit_native.group_report(scans[0], interval),
                         ao_evidence_audit_native.group_report(scans[1], interval))
        self.assertEqual(len(ao_evidence_audit_native.launch_candidates(scans[0], interval)),
                         len(ao_evidence_audit_native.launch_candidates(scans[1], interval)))
        self.assertEqual(scans[0].stop_rows, [])
        self.assertTrue(scans[1].stop_rows)
        self.assertIsNone(scans[0].tool_entries["toolu_1"].occurrences[0].identity)
        self.assertIsNotNone(scans[1].tool_entries["toolu_1"].occurrences[0].identity)
        annotated = ao_evidence_audit_native.launch_inventory(scans[0], interval)
        self.assertEqual([candidate.tool_id for candidate in annotated], ["toolu_1"])

    def test_default_read_audit_report_output_is_unchanged(self):
        self.room.write_parent([self.room.human()])
        state = self.room.new_state(bindings=False)
        (self.room.directory / "state.json").write_text(json.dumps(state, indent=2, sort_keys=True))
        report = ao_evidence_audit.audit(self.room.home, self.room.room_id, self.room.request_id,
                                         self.room.database, self.room.workspace)
        self.assertEqual(set(report), TOP_KEYS)
        self.assertEqual(report["version"], 1)
        self.assertEqual(report["limitations"], LIMITATIONS)
        self.assertEqual(report["redundant_read_verdict"], "not_established")
        self.assertEqual(report["coverage"], "complete")
        self.assertEqual(report["parent"]["counts"], dict.fromkeys(COUNT_KEYS, 0))
        self.assertEqual(report["children"], [])
        self.assertEqual(report["child_coverage"], "complete")
        self.assertEqual(report["reasons"], [])
        self.assertEqual(report["request_sha256"], hashlib.sha256(self.room.request_id.encode()).hexdigest())


if __name__ == "__main__":
    unittest.main()
