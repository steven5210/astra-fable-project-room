"""W3 real-Service integration: first sync, capture, acceptance and concrete correlation cases.

This module uses the actual shared Service.ao_room_sync, the actual preparation validation, the
reviewed W2 reader, the real bounded collector and real temporary SQL/native ownership. Only the AO
transport is the supplied in-process fake. No model, provider, network, live account or private
transcript is used, and nothing here claims a run or a served identity.

``WorkerIdentityHistoricalRefreshTests`` is a helper-only composition of the reviewed routing-refresh
fixture, not the real-SQL fixture above: that reviewed fixture builds its own SQL owner for the
committed refresh while its charter request keeps the fixture's disclosed mocked owner reader. The
composition proves W3 consumes its own historical W2 reference after a real committed refresh; it is
not claimed as real-SQL ownership evidence.
"""

import copy
import datetime
import json
import shutil
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

import ao_model_boundaries
import ao_native_outcome
import ao_outcomes
import ao_project_room as ao
import ao_worker_identity
from room import RoomError
from test_ao_normal import Fixture


class ServiceWorkerFixture(Fixture):
    """Normal room with actual SQLite ownership and a transcript in the prepared config root."""

    NATIVE = "native-w3-service"

    def register_native_source(self):
        if getattr(self, "native_outcome_source", None) is not None:
            return
        state = self.state()
        binding = (state.get("bindings") or {}).get("engineer")
        if not binding:
            return
        self.database = self.root / "synthetic-ao.db"
        with sqlite3.connect(self.database) as db:
            db.executescript(
                "CREATE TABLE sessions(id TEXT, project_id TEXT, harness TEXT, session_mode TEXT,"
                " is_terminated INTEGER, activity_state TEXT, workspace_path TEXT,"
                " provider_conversation_id TEXT, controller_generation TEXT);"
                "CREATE TABLE conversations(id TEXT, current_session_id TEXT, active_branch_id TEXT);"
                "CREATE TABLE conversation_branches(id TEXT, conversation_id TEXT, provider_conversation_id TEXT,"
                " session_id TEXT, strategy TEXT, replay_truncated INTEGER);")
            db.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?)",
                       (binding["session_id"], state["ao_project_id"], "claude-code", "chat", 0, "idle",
                        str(self.repo), self.NATIVE, "generation-one"))
            db.execute("INSERT INTO conversations VALUES(?,?,?)",
                       (binding["conversation_id"], binding["session_id"], binding["branch_id"]))
            db.execute("INSERT INTO conversation_branches VALUES(?,?,?,?,?,?)",
                       (binding["branch_id"], binding["conversation_id"], self.NATIVE,
                        binding["session_id"], "native", 0))
        self.database.chmod(0o600)
        config_root = self.native_config_root()
        project_dir = config_root / "projects" / str(self.repo).replace("/", "-")
        self.transcript = project_dir / (self.NATIVE + ".jsonl")
        project_dir.mkdir(parents=True, exist_ok=True)
        self.transcript.write_text("")
        self.native_events = []
        self.native_requests = set()
        source = {"database": str(self.database), "transcript": str(self.transcript),
                  "session_id": binding["session_id"], "native_session_id": self.NATIVE}
        ao_native_outcome.preflight_source(self.directory(), state, source["database"], source["transcript"])
        ao.atomic(self.directory() / "state.json", state)
        self.native_outcome_source = source

    def stamp_seconds(self, seconds):
        """A bounded synthetic instant for the implementation request's native timeline.

        The intended offset from the request's own creation is kept while that instant is already in
        the past and is otherwise clamped to this moment, so no synthetic row is future-dated past
        the AO completion and the receipt observation. No sleep or early terminal-state save is used.
        """
        request = self.state()["requests"]["implementation"]
        return min(request["created_at"] + seconds,
                   datetime.datetime.now(datetime.timezone.utc).timestamp())

    def iso_at(self, seconds):
        return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).isoformat()

    def stamp(self, seconds):
        return self.iso_at(self.stamp_seconds(seconds))

    def append_parent_rows(self, rows):
        self.native_events.extend(rows)
        self.transcript.write_text("".join(json.dumps(row) + "\n" for row in self.native_events))

    def launch_row(self, tool_id, uuid_value, timestamp, role="pr-opus", prompt="Do work"):
        return {"type": "assistant", "sessionId": self.NATIVE, "cwd": str(self.repo),
                "uuid": uuid_value, "timestamp": timestamp, "isSidechain": False,
                "message": {"role": "assistant", "model": self.QUALIFIED_MODEL,
                            "content": [{"type": "tool_use", "id": tool_id, "name": "Agent",
                                         "input": {"subagent_type": role, "prompt": prompt,
                                                   "run_in_background": False}}]}}

    def result_row(self, tool_id, uuid_value, timestamp, launcher, structured, source=None, extra_blocks=()):
        block = {"type": "tool_result", "tool_use_id": tool_id, "content": "done", "is_error": False}
        return {"type": "user", "sessionId": self.NATIVE, "cwd": str(self.repo),
                "uuid": uuid_value, "timestamp": timestamp, "isSidechain": False,
                "sourceToolAssistantUUID": source if source is not None else launcher,
                "message": {"role": "user", "content": [block, *extra_blocks]},
                "toolUseResult": structured}

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

    def child_row(self, agent_id, uuid_value, timestamp, model, source=None, api_error=False):
        """One child assistant row.

        The observed native schema normally omits ``sourceToolAssistantUUID`` on the first child
        rows; an explicit ``source`` writes that pointer only when a test needs that shape.
        """
        row = {"type": "assistant", "sessionId": self.NATIVE, "cwd": str(self.repo),
               "uuid": uuid_value, "timestamp": timestamp, "isSidechain": True, "agentId": agent_id,
               "message": {"role": "assistant", "model": model,
                           "content": [{"type": "text", "text": "worker done"}],
                           "stop_reason": "end_turn"}}
        if source is not None:
            row["sourceToolAssistantUUID"] = source
        if api_error:
            row["isApiErrorMessage"] = True
            row["error"] = "rate_limit"
        return row

    def child_local_result_row(self, agent_id, uuid_value, timestamp, assistant_uuid,
                               tool_id="toolu_child_1"):
        """One child-local user tool-result row pointing at this child's own assistant message."""
        return {"type": "user", "sessionId": self.NATIVE, "cwd": str(self.repo), "uuid": uuid_value,
                "timestamp": timestamp, "isSidechain": True, "agentId": agent_id,
                "sourceToolAssistantUUID": assistant_uuid,
                "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id,
                                                         "content": "child local result",
                                                         "is_error": False}]}}

    def write_child(self, agent_id, rows):
        path = Path(self.transcript).parent / self.NATIVE / "subagents" / ("agent-" + agent_id + ".jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        return path

    def semantic_record(self, request):
        """The retained semantic outcome record of one completed request."""
        return ao_outcomes.load(self.directory(), request)

    def worker_evidence(self, request):
        """The retained worker observation of one completed request, from its own semantic record."""
        return self.semantic_record(request).get("worker_observations")

    def implementation_with_child(self, child_model=None, result_source=None, shared_projection=False,
                                  task_id=None, child_api_error=False, child_count=1,
                                  child_local_result=False):
        self.bind()
        self.agree()
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.send("implementation")
        # The implementation is written before the completed turn is captured, so the candidate the
        # projection captures is exactly the one the later gates and acceptance observe.
        (self.repo / "feature.txt").write_text("implemented\n")
        # Native source order: the caller packet, this request's own tool sequence, then the parent
        # final. The shared fixture records the caller/response pair at dispatch, so keep the caller
        # where it is and re-append the one synthetic parent final after the tool rows.
        final = self.native_events.pop()
        self.assertEqual(final.get("uuid"), "implementation-reply")
        parent_rows = []
        children = {}
        for index in range(child_count):
            tool = "toolu_w3_%d" % index
            launch = "launch-w3-%d" % index
            agent = "agent-w3-%d" % index
            result = "result-w3-%d" % index
            launch_at = self.stamp_seconds(0.003 + index * 0.01)
            result_at = max(self.stamp_seconds(0.005 + index * 0.01), launch_at)
            parent_rows.append(self.launch_row(tool, launch, self.iso_at(launch_at)))
            extra = () if not shared_projection else (
                {"type": "tool_result", "tool_use_id": "read-w3", "content": "x"},)
            parent_rows.append(self.result_row(tool, result, self.iso_at(result_at), launch,
                                               self.completed(agent, task_id=task_id),
                                               source=result_source, extra_blocks=extra))
            model = self.QUALIFIED_WORKERS["pr-opus"] if child_model is None else child_model
            child_at = launch_at + (result_at - launch_at) / 2.0
            rows = [self.child_row(agent, "child-w3-%d" % index, self.iso_at(child_at), model,
                                   api_error=child_api_error)]
            if child_local_result:
                rows.append(self.child_local_result_row(agent, "child-w3-%d-local" % index,
                                                        self.iso_at(child_at), "child-w3-%d" % index))
            children[agent] = rows
        # A zero-launch turn has no child window, so its own final row still needs a positive
        # ordered instant after the caller and before the observed completion; genuine-child
        # intervals keep their established offsets.
        final["timestamp"] = self.stamp(0.007 + max(child_count - 1, 0) * 0.01)
        self.append_parent_rows([*parent_rows, final])
        for agent, rows in children.items():
            self.write_child(agent, rows)
        self.fake.finish("engineer", json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        return self.state()["requests"]["implementation"]


class WorkerIdentityServiceTests(ServiceWorkerFixture):
    def setUp(self):
        super().setUp()
        self.room = self.open()
        self.spec()

    def test_matching_worker_allows_capture_and_acceptance_on_first_completion(self):
        request = self.implementation_with_child()
        self.assertNotIn("engineering_error", request)
        self.assertIn("engineering_record", request)
        self.assertTrue(self.worker_evidence(request)["qualified"])
        self.service.ao_room_verify(self.room, str(self.repo))
        self.send("acceptance_review")
        review = self.state()["requests"]["acceptance_review"]
        self.fake.finish("reviewer", json.dumps({**review["review"], "decision": "approved",
                                                  "review": "Independently inspected exact behavior and gates."}))
        self.service.ao_room_sync(self.room)
        self.assertTrue(self.service.ao_room_accept(self.room, "acceptance_review")["accepted"])

    def test_mismatched_worker_withholds_capture_and_usability_on_first_completion(self):
        request = self.implementation_with_child(child_model="claude-opus-5-1")
        self.assertNotIn("engineering_record", request)
        self.assertIn("engineering_error", request)
        evidence = self.worker_evidence(request)
        self.assertFalse(evidence["qualified"])
        self.assertIn("child_model_mismatch", evidence["reasons"])
        with self.assertRaises(RoomError):
            ao_outcomes.usable(self.directory(), request)
        with self.assertRaises(RoomError):
            self.service.ao_room_verify(self.room, str(self.repo))

    def test_foreign_result_launcher_is_not_successful_correlation(self):
        request = self.implementation_with_child(result_source="foreign-launcher")
        evidence = self.worker_evidence(request)
        self.assertFalse(evidence["qualified"])
        self.assertIn("child_attribution_ambiguous", evidence["reasons"])

    def test_shared_result_projection_is_not_successful_correlation(self):
        request = self.implementation_with_child(shared_projection=True)
        evidence = self.worker_evidence(request)
        self.assertFalse(evidence["qualified"])
        self.assertIn("child_attribution_ambiguous", evidence["reasons"])

    def test_malformed_task_alias_is_not_successful_correlation(self):
        request = self.implementation_with_child(task_id=123)
        evidence = self.worker_evidence(request)
        self.assertFalse(evidence["qualified"])
        self.assertIn("child_attribution_ambiguous", evidence["reasons"])

    def test_native_omitted_child_pointer_with_child_local_result_qualifies(self):
        # The observed child schema: the first child assistant row omits ``sourceToolAssistantUUID``
        # and a later child-local user tool-result row points at that child's own assistant message.
        # Neither is parent ownership evidence, and neither is a foreign cross-actor reference.
        request = self.implementation_with_child(child_local_result=True)
        evidence = self.worker_evidence(request)
        self.assertTrue(evidence["qualified"])
        self.assertEqual(evidence["executions"][0]["agent_id"], "agent-w3-0")
        self.assertNotIn("child_attribution_ambiguous", evidence["reasons"])

    def test_error_only_child_row_never_proves_a_served_model(self):
        request = self.implementation_with_child(child_api_error=True)
        evidence = self.worker_evidence(request)
        self.assertFalse(evidence["qualified"])
        self.assertIn("child_model_missing", evidence["reasons"])
        self.assertEqual(evidence["observed_models"], [])

    def test_child_bounds_are_enforced_before_opening_beyond_the_ceiling(self):
        request = self.implementation_with_child(child_count=33)
        observations = self.worker_evidence(request)
        self.assertFalse(observations["qualified"])
        self.assertIn("child_limit", observations["reasons"])
        self.assertEqual(len(observations["launches"]), 33)
        self.assertEqual(len(observations["executions"]), 32)

    def test_double_deletion_with_unchanged_receipt_refuses_provenance(self):
        request = self.implementation_with_child()
        record = self.semantic_record(request)
        self.assertTrue(record["worker_observations"]["qualified"])
        ao_outcomes.usable(self.directory(), request)  # the unmodified qualified record stays usable
        receipt_path = self.directory() / request["receipt"]
        before = receipt_path.read_bytes()
        # A copied retained request with both modern fields deleted, against the unchanged receipt.
        stripped = copy.deepcopy(request)
        stripped.pop("native_worker_expectations", None)
        stripped.pop("prompt_projection", None)
        value = ao_worker_identity.observe_workers(self.directory(), self.state(), stripped,
                                                   ao.read(receipt_path), record["native"])
        self.assertIsNotNone(value)
        self.assertFalse(value["qualified"])
        self.assertEqual(value["reason"], "worker_expectation_provenance")
        with self.assertRaises(RoomError):
            ao_outcomes.usable(self.directory(), stripped)  # cached usability authenticates provenance
        self.assertEqual(receipt_path.read_bytes(), before)

    def test_genuine_pre_routing_absence_keeps_the_old_path(self):
        self.bind()
        # An explicit pre-routing room: the preparation record carries no native routing record
        # before this room's first request, exactly as an old room does. No request field is
        # deleted later and no aggregate reader is stubbed to manufacture history.
        prepared = ao.read(self.directory() / "preparation.json")
        prepared.pop("routing", None)
        ao.atomic(self.directory() / "preparation.json", prepared)
        state = self.state()
        state["preparation_sha256"] = ao.digest(prepared)
        ao.atomic(self.directory() / "state.json", state)
        self.agree()
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.send("implementation")
        self.fake.finish("engineer", json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        request = self.state()["requests"]["implementation"]
        self.assertNotIn("native_worker_expectations", request)
        absent = ao_model_boundaries.read(self.directory(), self.state(), request)
        self.assertEqual((absent["status"], absent["reason"]),
                         ("absent", "no_frozen_worker_expectations"))
        record = ao_outcomes.load(self.directory(), request)
        self.assertNotIn("worker_observations", record)
        self.assertIn("engineering_record", request)


class WorkerIdentityRelocationTests(ServiceWorkerFixture):
    """Actual Service relocation coverage: registered parent moves, prepared child authority, drift."""

    def setUp(self):
        super().setUp()
        self.room = self.open()
        self.spec()

    def audit_native(self, transcript):
        return self.service.ao_room_outcome_audit(self.room, ao_database_path=str(self.database),
                                                  native_transcript_path=str(transcript))

    def baseline(self):
        request = self.implementation_with_child()
        record = self.semantic_record(request)
        self.assertTrue(record["worker_observations"]["qualified"])
        return request, record

    def relocate_source(self, name, keep_original=True):
        target_dir = self.root / name
        target_dir.mkdir()
        target = target_dir / self.transcript.name
        target.write_bytes(self.transcript.read_bytes())
        if not keep_original:
            self.transcript.unlink()
        return target

    def adopt_registered_source(self):
        self.transcript = Path(self.state()["native_outcome_source"]["transcript"])
        return self.transcript

    def child_file(self, agent_id):
        return Path(self.transcript).parent / self.NATIVE / "subagents" / ("agent-" + agent_id + ".jsonl")

    def remove_child_lineage(self):
        folder = Path(self.transcript).parent / self.NATIVE
        if folder.exists():
            shutil.rmtree(folder)

    def test_external_copy_with_original_retained_keeps_qualified_worker_and_evidence(self):
        request, record = self.baseline()
        receipt_path = self.directory() / request["receipt"]
        receipt_bytes = receipt_path.read_bytes()
        outcome_path = self.directory() / request["semantic_outcome"]
        outcome_bytes = outcome_path.read_bytes()
        expectation = copy.deepcopy(request["native_worker_expectations"])
        posts = len(self.fake.posts)
        moved = self.relocate_source("moved-retained")
        result = self.audit_native(moved)
        self.assertEqual(len(self.fake.posts), posts)
        self.assertEqual(result["outcome"]["kind"], "final_available")
        self.assertFalse(result["outcome"]["hold"])
        current = self.state()["requests"]["implementation"]
        evidence = self.worker_evidence(current)
        self.assertTrue(evidence["qualified"])
        self.assertEqual(evidence["executions"][0]["agent_id"], "agent-w3-0")
        self.assertEqual(evidence["executions"][0]["model"], self.QUALIFIED_WORKERS["pr-opus"])
        self.assertEqual(evidence["parent"]["source_sha256"], record["native"]["source_sha256"])
        self.assertEqual(current["native_worker_expectations"], expectation)
        self.assertEqual(receipt_path.read_bytes(), receipt_bytes)
        self.assertEqual(outcome_path.read_bytes(), outcome_bytes)
        self.assertTrue(self.transcript.exists())
        self.adopt_registered_source()
        posts = len(self.fake.posts)
        self.send("correction", "post-move-followup")
        self.assertEqual(len(self.fake.posts), posts + 1)
        self.send("correction", "post-move-followup")
        self.assertEqual(len(self.fake.posts), posts + 1)

    def test_external_copy_with_original_deleted_before_audit_uses_prepared_lineage(self):
        request, record = self.baseline()
        moved = self.relocate_source("moved-deleted", keep_original=False)
        posts = len(self.fake.posts)
        result = self.audit_native(moved)
        self.assertEqual(len(self.fake.posts), posts)
        self.assertFalse(result["outcome"]["hold"])
        current = self.state()["requests"]["implementation"]
        evidence = self.worker_evidence(current)
        self.assertTrue(evidence["qualified"])
        self.assertEqual(evidence["executions"][0]["agent_id"], "agent-w3-0")
        self.assertEqual(evidence["parent"]["source_sha256"], record["native"]["source_sha256"])
        self.assertFalse(self.transcript.exists())
        self.assertTrue(self.child_file("agent-w3-0").exists())

    def test_external_copy_with_original_deleted_after_audit_keeps_followup_qualified(self):
        request, record = self.baseline()
        moved = self.relocate_source("moved-after-audit")
        original = self.transcript
        result = self.audit_native(moved)
        self.assertFalse(result["outcome"]["hold"])
        original.unlink()
        self.adopt_registered_source()
        posts = len(self.fake.posts)
        self.service.ao_room_sync(self.room)
        current = self.state()["requests"]["implementation"]
        evidence = self.worker_evidence(current)
        self.assertTrue(evidence["qualified"])
        self.send("correction", "after-delete-followup")
        self.assertEqual(len(self.fake.posts), posts + 1)

    def test_zero_launch_external_copy_without_child_lineage_still_qualifies(self):
        request = self.implementation_with_child(child_count=0)
        record = self.semantic_record(request)
        self.assertEqual(record["outcome"]["kind"], "final_available")
        self.assertIs(record["outcome"]["hold"], False)
        self.assertTrue(record["worker_observations"]["qualified"])
        self.assertEqual(record["worker_observations"]["executions"], [])
        moved = self.relocate_source("moved-zero", keep_original=False)
        self.remove_child_lineage()
        result = self.audit_native(moved)
        self.assertFalse(result["outcome"]["hold"])
        current = self.state()["requests"]["implementation"]
        evidence = self.worker_evidence(current)
        self.assertTrue(evidence["qualified"])
        self.assertEqual(evidence["executions"], [])
        self.assertEqual(evidence["parent"]["source_sha256"], record["native"]["source_sha256"])

    def test_missing_child_lineage_never_manufactures_launch_absence(self):
        request, record = self.baseline()
        moved = self.relocate_source("moved-missing-child", keep_original=False)
        self.remove_child_lineage()
        result = self.audit_native(moved)
        self.assertTrue(result["outcome"]["hold"])
        evidence = self.worker_evidence(self.state()["requests"]["implementation"])
        self.assertFalse(evidence["qualified"])
        self.assertIn("child_missing", evidence["reasons"])
        self.assertEqual(evidence["executions"], [])
        self.assertEqual(len(evidence["launches"]), 1)

    def test_external_adjacent_child_decoy_is_never_used(self):
        request, record = self.baseline()
        moved = self.relocate_source("moved-decoy", keep_original=False)
        self.remove_child_lineage()
        decoy = moved.parent / self.NATIVE / "subagents" / "agent-w3-0.jsonl"
        decoy.parent.mkdir(parents=True)
        decoy.write_text(json.dumps(self.child_row("agent-w3-0", "child-decoy",
                                                   self.iso_at(self.stamp_seconds(0.004)),
                                                   "claude-opus-5-1")) + "\n")
        self.audit_native(moved)
        evidence = self.worker_evidence(self.state()["requests"]["implementation"])
        self.assertFalse(evidence["qualified"])
        self.assertIn("child_missing", evidence["reasons"])

    def test_ambiguous_native_id_lineages_refuse_without_guessing(self):
        request, record = self.baseline()
        moved = self.relocate_source("moved-ambiguous", keep_original=False)
        duplicate = Path(self.transcript).parent.parent / "other-project" / self.NATIVE / "subagents"
        duplicate.mkdir(parents=True)
        shutil.copy2(self.child_file("agent-w3-0"), duplicate / "agent-w3-0.jsonl")
        self.audit_native(moved)
        evidence = self.worker_evidence(self.state()["requests"]["implementation"])
        self.assertFalse(evidence["qualified"])
        self.assertIn("identity_conflict", evidence["reasons"])

    def test_duplicate_canonical_parents_refuse(self):
        request, record = self.baseline()
        duplicate = Path(self.transcript).parent.parent / "other-parent" / self.transcript.name
        duplicate.parent.mkdir(parents=True)
        shutil.copy2(self.transcript, duplicate)
        moved = self.relocate_source("moved-duplicate-parent")
        self.audit_native(moved)
        evidence = self.worker_evidence(self.state()["requests"]["implementation"])
        self.assertFalse(evidence["qualified"])
        self.assertIn("identity_conflict", evidence["reasons"])

    def test_symlinked_native_id_lineage_refuses(self):
        request, record = self.baseline()
        moved = self.relocate_source("moved-symlink", keep_original=False)
        original = Path(self.transcript).parent / self.NATIVE
        shutil.rmtree(original)
        original.symlink_to(moved.parent / self.NATIVE, target_is_directory=True)
        self.audit_native(moved)
        evidence = self.worker_evidence(self.state()["requests"]["implementation"])
        self.assertFalse(evidence["qualified"])
        self.assertIn("source_unsafe", evidence["reasons"])

    def test_unique_move_within_prepared_projects_keeps_child_lineage(self):
        request, record = self.baseline()
        source = self.transcript
        target_dir = source.parent.parent / "moved-project"
        target_dir.mkdir()
        shutil.move(str(source.parent / self.NATIVE), str(target_dir / self.NATIVE))
        shutil.move(str(source), str(target_dir / source.name))
        result = self.audit_native(target_dir / source.name)
        self.assertFalse(result["outcome"]["hold"])
        evidence = self.worker_evidence(self.state()["requests"]["implementation"])
        self.assertTrue(evidence["qualified"])
        self.assertEqual(evidence["executions"][0]["agent_id"], "agent-w3-0")

    def test_parent_drift_between_complete_passes_refuses(self):
        request, record = self.baseline()
        real = ao_worker_identity.audit_io.rehash_source
        def drift(opened, result, collector, limits):
            value = real(opened, result, collector, limits)
            if opened.name == self.transcript.name:
                self.transcript.write_text(self.transcript.read_text() + "\n")
            return value
        with mock.patch.object(ao_worker_identity.audit_io, "rehash_source", side_effect=drift):
            self.audit_native(self.transcript)
        evidence = self.worker_evidence(self.state()["requests"]["implementation"])
        self.assertFalse(evidence["qualified"])
        self.assertIn("source_changed", evidence["reasons"])

    def test_child_drift_between_complete_passes_refuses(self):
        request, record = self.baseline()
        real = ao_worker_identity.audit_io.rehash_source
        def drift(opened, result, collector, limits):
            value = real(opened, result, collector, limits)
            if opened.name.startswith("agent-"):
                path = self.child_file("agent-w3-0")
                path.write_text(path.read_text() + "\n")
            return value
        with mock.patch.object(ao_worker_identity.audit_io, "rehash_source", side_effect=drift):
            self.audit_native(self.transcript)
        evidence = self.worker_evidence(self.state()["requests"]["implementation"])
        self.assertFalse(evidence["qualified"])
        self.assertIn("source_changed", evidence["reasons"])

    def test_replaced_native_id_directory_with_original_subagents_refuses(self):
        request, record = self.baseline()
        moved = self.relocate_source("moved-lineage-replaced", keep_original=False)
        self.assertFalse(self.transcript.exists())
        native_directory = Path(self.transcript).parent / self.NATIVE
        subagents = native_directory / "subagents"
        subagents_inode = subagents.stat().st_ino
        retained = self.root / "retained-native-directory"
        real = ao_worker_identity.audit_io.rehash_source
        replaced = []

        def replace(opened, result, collector, limits):
            value = real(opened, result, collector, limits)
            if opened.name == self.NATIVE + ".jsonl" and not replaced:
                replaced.append(native_directory.stat().st_ino)
                native_directory.rename(retained)
                native_directory.mkdir()
                shutil.move(str(retained / "subagents"), str(subagents))
            return value

        with mock.patch.object(ao_worker_identity.audit_io, "rehash_source", side_effect=replace):
            result = self.audit_native(moved)
        self.assertTrue(replaced)
        self.assertNotEqual(native_directory.stat().st_ino, replaced[0])
        self.assertEqual(subagents.stat().st_ino, subagents_inode)
        self.assertTrue(result["outcome"]["hold"])
        evidence = self.worker_evidence(self.state()["requests"]["implementation"])
        self.assertFalse(evidence["qualified"])
        self.assertIn("source_changed", evidence["reasons"])

    def test_replaced_project_directory_preserving_native_lineage_refuses(self):
        request, record = self.baseline()
        moved = self.relocate_source("moved-project-lineage", keep_original=False)
        self.assertFalse(self.transcript.exists())
        project_directory = Path(self.transcript).parent
        native_directory = project_directory / self.NATIVE
        native_inode = native_directory.stat().st_ino
        retained = self.root / "retained-project-directory"
        real = ao_worker_identity.audit_io.rehash_source
        replaced = []

        def replace(opened, result, collector, limits):
            value = real(opened, result, collector, limits)
            if opened.name == self.NATIVE + ".jsonl" and not replaced:
                replaced.append(project_directory.stat().st_ino)
                project_directory.rename(retained)
                project_directory.mkdir()
                shutil.move(str(retained / self.NATIVE), str(native_directory))
            return value

        with mock.patch.object(ao_worker_identity.audit_io, "rehash_source", side_effect=replace):
            result = self.audit_native(moved)
        self.assertTrue(replaced)
        self.assertNotEqual(project_directory.stat().st_ino, replaced[0])
        self.assertEqual(native_directory.stat().st_ino, native_inode)
        self.assertTrue(result["outcome"]["hold"])
        evidence = self.worker_evidence(self.state()["requests"]["implementation"])
        self.assertFalse(evidence["qualified"])
        self.assertIn("source_changed", evidence["reasons"])

    def test_new_native_id_lineage_between_discovery_and_terminal_recheck_refuses(self):
        request, record = self.baseline()
        real = ao_worker_identity.audit_io.rehash_source
        def add_lineage(opened, result, collector, limits):
            value = real(opened, result, collector, limits)
            if opened.name == self.NATIVE + ".jsonl":
                second = Path(self.transcript).parent.parent / "late-project" / self.NATIVE / "subagents"
                second.mkdir(parents=True, exist_ok=True)
            return value
        with mock.patch.object(ao_worker_identity.audit_io, "rehash_source", side_effect=add_lineage):
            self.audit_native(self.transcript)
        evidence = self.worker_evidence(self.state()["requests"]["implementation"])
        self.assertFalse(evidence["qualified"])
        self.assertIn("source_changed", evidence["reasons"])

    def test_changed_native_owner_refuses_before_worker_success(self):
        self.baseline()
        with sqlite3.connect(self.database) as db:
            db.execute("UPDATE sessions SET provider_conversation_id='foreign-native'")
            db.execute("UPDATE conversation_branches SET provider_conversation_id='foreign-native'")
        with self.assertRaises(RoomError):
            self.audit_native(self.transcript)


class WorkerIdentityHistoricalRefreshTests(unittest.TestCase):
    """W3 consumes its own historical W2 reference after a real committed routing refresh.

    Helper-only composition of the reviewed routing-refresh fixture, the same composition the
    reviewed RefreshBoundaryTests use: the real handoff, packet assembly, sync, capture and
    committed refresh run against the in-process fake AO, and W3 then consumes the historical
    request's own frozen reference. That fixture's charter source keeps its disclosed mocked owner
    reader while the refresh uses the fixture's own SQL owner database, so this composition is not
    claimed as real-SQL ownership evidence.
    """

    def setUp(self):
        import test_ao_routing_refresh as refresh_tests
        self.case = refresh_tests.RoutingRefreshTests("runTest")
        self.addCleanup(self.case.doCleanups)
        self.case.setUp()

    def test_historical_request_keeps_its_own_frozen_expectation_after_a_refresh(self):
        case = self.case
        case.service.ao_room_handoff(case.room, str(case.repo))
        case.send("implementation")
        case.fake.finish("engineer", json.dumps(case.report()))
        case.service.ao_room_sync(case.room)
        saved = copy.deepcopy(case.state()["requests"]["implementation"])
        case.do_refresh()
        state = case.state()
        self.assertIn("routing_refresh", state)  # the real refresh committed before W3 reads history
        retained = state["requests"]["implementation"]
        self.assertEqual(retained["native_worker_expectations"], saved["native_worker_expectations"])
        record = ao.read(case.directory() / retained["semantic_outcome"])
        self.assertEqual(ao.digest(record), retained["semantic_outcome_sha256"])
        value = ao_worker_identity.observe_workers(case.directory(), state, retained,
                                                   ao.read(case.directory() / retained["receipt"]),
                                                   record["native"])
        self.assertIsNotNone(value)
        self.assertTrue(value["qualified"])
        self.assertEqual(value["expectation"]["authority"], {"kind": "preparation"})
        self.assertEqual(value["expectation"]["routing"]["sha256"],
                         saved["native_worker_expectations"]["routing_sha256"])


if __name__ == "__main__":
    unittest.main()
