"""W2c bounded-reader receipt authentication and source-backed boundary regressions.

Composed synthetic fixtures only. The normal-room fixture supplies the actual Service send, packet,
receipt and sync paths; the existing routing-refresh, qualified-refresh and compaction fixtures are
instantiated through their own modules with 'runTest' and their cleanups registered here, so their
unrelated contract tests do not run again. Every boundary call runs the real bounded selected-receipt
reader and the real routing, qualification, settlement and executable-binding validation against the
in-process fake AO. No AO process, account, socket, model or live room is used, and nothing here
claims a served model identity, effective effort, execution success, settlement authority or
acceptance.
"""

import copy
import json
import sys
import unittest
from unittest import mock

import ao_executable_binding
import ao_model_boundaries
import ao_model_qualification as qmod
import ao_outcomes
import ao_project_room as ao
import ao_prompt_metrics
import ao_routing
import ao_workflow
from room import RoomError
from test_ao_normal import Fixture


class ReceiptAuthenticationTests(Fixture):
    """The strict reader authenticates the request's own persisted receipt, never a plain JSON read."""

    def setUp(self):
        super().setUp()
        self.room = self.open()
        self.spec()

    def ready_implementation(self):
        self.bind()
        self.agree()
        self.service.ao_room_handoff(self.room, str(self.repo))

    def completed_implementation(self):
        self.ready_implementation()
        self.send("implementation")
        self.fake.finish("engineer", json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        return self.state()["requests"]["implementation"]

    def rewrite_preparation(self, mutate):
        directory, state = self.directory(), self.state()
        prepared = ao.read(directory / state["preparation"])
        mutate(prepared)
        ao.atomic(directory / state["preparation"], prepared)
        state["preparation_sha256"] = ao.digest(prepared)
        ao.atomic(directory / "state.json", state)

    def variant(self, request, mutate):
        """One internally content-addressed receipt variant, named by a copy of the request.

        Only the named field is falsified; the original receipt bytes are never touched and the copy
        carries the variant's own correct canonical pointer and digest, so only the reader's own
        identity, delivery, path and projection checks can refuse it.
        """
        directory = self.directory()
        value = copy.deepcopy(ao.read(directory / request["receipt"]))
        mutate(value)
        payload = {key: item for key, item in value.items() if key != "observed_at"}
        relative = "receipts/" + request["request_id"] + "/" + ao_prompt_metrics.digest(payload) + ".json"
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        ao.atomic(target, value)
        clone = copy.deepcopy(request)
        clone["receipt"] = relative
        clone["receipt_sha256"] = ao_prompt_metrics.digest(value)
        return clone

    def copied_state(self, request_id, clone):
        """One deep-copied in-memory state whose retained request is exactly the variant caller copy.

        read() intentionally authenticates the retained request from state and checks only the
        immutable identity of the caller, so a corruption negative must present the variant as the
        retained request. The on-disk state and the original request/receipt bytes are never edited.
        """
        state = copy.deepcopy(self.state())
        state["requests"][request_id] = copy.deepcopy(clone)
        return state

    def test_contradictory_receipt_identity_is_refused_against_the_actual_reader(self):
        request = self.completed_implementation()
        directory = self.directory()
        original = (directory / request["receipt"]).read_bytes()

        def model(value):
            value["settings"]["model"] = "claude-sonnet-5-5"

        def effort(value):
            value["settings"]["reasoningEffort"] = "low"

        def turn(value):
            value["turn"]["id"] = "turn-substituted"

        def provider(value):
            value["turn"]["providerTurnId"] = "native-substituted"

        for name, mutate in (("settings.model", model), ("settings.reasoningEffort", effort),
                             ("turn.id", turn), ("turn.providerTurnId", provider)):
            with self.subTest(field=name):
                clone = self.variant(request, mutate)
                self.assertFalse(ao_prompt_metrics._receipt_identity(clone, ao.read(directory / clone["receipt"])))
                variant_state = self.copied_state("implementation", clone)
                with self.assertRaisesRegex(RoomError, "completed observation authority"):
                    ao_model_boundaries.read(directory, variant_state, clone)
                self.assertEqual((directory / request["receipt"]).read_bytes(), original)
                self.assertEqual(self.state()["requests"]["implementation"]["receipt"], request["receipt"])
        rerouted = copy.deepcopy(request)
        rerouted["model_reroute"] = {"fromModel": request["model"], "toModel": "claude-opus-5-5"}
        rerouted_state = self.copied_state("implementation", rerouted)
        with self.assertRaisesRegex(RoomError, "completed observation authority"):
            ao_model_boundaries.read(directory, rerouted_state, rerouted)
        self.assertEqual((directory / request["receipt"]).read_bytes(), original)

    def test_unsupported_native_turn_states_are_refused_against_the_actual_reader(self):
        """Only completed and failed native receipt turns are supported observation authority."""
        request = self.completed_implementation()
        directory = self.directory()
        original = (directory / request["receipt"]).read_bytes()
        for native_state in ("recovered", "cancelled", "interrupted", "unsupported"):
            with self.subTest(native_turn_state=native_state):
                def mutate(value, native_state=native_state):
                    value["turn"]["state"] = native_state
                clone = self.variant(request, mutate)
                variant_state = self.copied_state("implementation", clone)
                with self.assertRaisesRegex(RoomError, "completed observation authority"):
                    ao_model_boundaries.read(directory, variant_state, clone)
                self.assertEqual((directory / request["receipt"]).read_bytes(), original)
        self.assertEqual(self.state()["requests"]["implementation"]["receipt"], request["receipt"])

    def test_stale_observational_caller_copy_still_reads_the_retained_request(self):
        """read() authenticates the retained request and only the immutable identity of the caller."""
        request = self.completed_implementation()
        directory = self.directory()
        before = (directory / request["receipt"]).read_bytes()
        stale = copy.deepcopy(request)
        stale["receipt"] = "receipts/stale/observational-caller.json"
        stale["receipt_sha256"] = "0" * 64
        stale["observed_turn"] = {"state": "completed", "providerTurnId": "stale-observation"}
        result = ao_model_boundaries.read(directory, self.state(), stale)
        self.assertEqual(result["status"], "frozen")
        self.assertEqual(result["agents"], self.QUALIFIED_WORKERS)
        self.assertEqual((directory / request["receipt"]).read_bytes(), before)
        self.assertEqual(self.state()["requests"]["implementation"]["receipt"], request["receipt"])

    def test_receipt_path_and_file_boundary_negatives_are_refused(self):
        request = self.completed_implementation()
        directory = self.directory()
        path = directory / request["receipt"]
        original = path.read_bytes()
        foreign = copy.deepcopy(request)
        foreign["receipt"] = "receipts/another-request/" + path.name
        foreign_state = self.copied_state("implementation", foreign)
        with self.assertRaisesRegex(RoomError, "pointer is unsafe"):
            ao_model_boundaries.read(directory, foreign_state, foreign)
        elsewhere = directory / "receipts" / "escaped.json"
        elsewhere.write_bytes(original)
        path.unlink()
        path.symlink_to(elsewhere)
        with self.assertRaisesRegex(RoomError, "Selected receipt"):
            ao_model_boundaries.read(directory, self.state(), request)
        self.assertEqual(elsewhere.read_bytes(), original)
        path.unlink()
        path.write_bytes(original)
        value = json.loads(original.decode())
        value["settings"]["reasoningEffort"] = "low"
        path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
        with self.assertRaisesRegex(RoomError, "saved canonical identity"):
            ao_model_boundaries.read(directory, self.state(), request)
        self.assertEqual(self.state()["requests"]["implementation"]["receipt_sha256"],
                         request["receipt_sha256"])

    def test_failed_native_receipt_before_settlement_is_authenticated_without_becoming_success(self):
        self.ready_implementation()
        self.send("implementation")
        self.fake.finish("engineer", json.dumps(self.report()), state="failed")
        self.service.ao_room_sync(self.room)
        request = self.state()["requests"]["implementation"]
        self.assertEqual(request["state"], "uncertain")
        self.assertNotIn("outcome_resume", request)
        receipt = ao.read(self.directory() / request["receipt"])
        self.assertEqual(receipt["turn"]["state"], "failed")
        self.assertTrue(ao_prompt_metrics._receipt_identity(request, receipt))
        self.assertTrue(ao_prompt_metrics._observed_delivery(request, receipt))
        original = (self.directory() / request["receipt"]).read_bytes()
        result = ao_model_boundaries.read(self.directory(), self.state(), request)
        self.assertEqual((result["status"], result["authority"]), ("frozen", {"kind": "preparation"}))
        self.assertEqual(result["agents"], self.QUALIFIED_WORKERS)
        self.assertEqual(self.state()["requests"]["implementation"]["state"], "uncertain")
        self.assertNotIn("outcome_resume", self.state()["requests"]["implementation"])
        self.assertEqual((self.directory() / request["receipt"]).read_bytes(), original)

    def test_failed_receipt_of_a_genuinely_absent_legacy_request_still_reads_as_absent(self):
        self.bind()
        self.rewrite_preparation(lambda prepared: prepared.pop("routing"))
        self.send("spec_review", "spec_review")
        spec = self.service.spec(self.directory(), self.state())
        self.fake.finish("engineer", json.dumps({"interpretation": "Failed legacy review.", "findings": [],
                                                 "decision": "accept", "spec_revision": 1,
                                                 "spec_sha256": spec["sha256"]}), state="failed")
        self.service.ao_room_sync(self.room)
        request = self.state()["requests"]["spec_review"]
        self.assertNotIn("native_worker_expectations", request)
        self.assertEqual(request["state"], "uncertain")
        self.assertEqual(ao.read(self.directory() / request["receipt"])["turn"]["state"], "failed")
        result = ao_model_boundaries.read(self.directory(), self.state(), request)
        self.assertEqual((result["status"], result["reason"]),
                         ("absent", "no_frozen_worker_expectations"))

    def test_first_sync_reads_the_guarded_in_memory_request_before_the_state_save(self):
        self.ready_implementation()
        self.send("implementation")
        self.fake.finish("engineer", json.dumps(self.report()))
        captured, original = {}, self.service.save

        def save(directory, state):
            request = state["requests"]["implementation"]
            if "result" not in captured and "receipt" in request:
                persisted = ao.read(directory / "state.json")["requests"]["implementation"]
                captured["persisted_receipt"] = persisted.get("receipt")
                captured["result"] = ao_model_boundaries.read(directory, state, request)
            return original(directory, state)

        with mock.patch.object(self.service, "save", side_effect=save):
            self.service.ao_room_sync(self.room)
        self.assertIn("result", captured)
        self.assertIsNone(captured["persisted_receipt"])
        self.assertEqual(captured["result"]["status"], "frozen")
        self.assertIn("receipt", self.state()["requests"]["implementation"])


class SupportedSettlementReceiptTests(unittest.TestCase):
    """A supported settlement keeps its own proof; a failed receipt is never read as success."""

    def setUp(self):
        import test_ao_compaction_failure as compaction_tests
        self.case = compaction_tests.CompactionServiceFixture("runTest")
        self.addCleanup(self.case.doCleanups)
        self.case.setUp()

    def test_failed_receipt_reads_before_settlement_and_its_settled_proof_afterwards(self):
        case, fixture = self.case, self.case.fixture
        request = fixture.state()["requests"][case.request_id]
        self.assertEqual(request["state"], "uncertain")
        self.assertNotIn("outcome_resume", request)
        self.assertEqual(ao.read(fixture.directory() / request["receipt"])["turn"]["state"], "failed")
        before = (fixture.directory() / request["receipt"]).read_bytes()
        failed = ao_model_boundaries.read(fixture.directory(), fixture.state(), request)
        self.assertEqual((failed["status"], failed["authority"]), ("frozen", {"kind": "preparation"}))
        self.assertEqual(fixture.state()["requests"][case.request_id]["state"], "uncertain")
        self.assertEqual((fixture.directory() / request["receipt"]).read_bytes(), before)
        notices = (request.get("carried") or {}).get("boundary_notices") or []
        self.assertEqual(len(notices), 1)
        case.settle()
        settled = fixture.state()["requests"][case.request_id]
        self.assertEqual(settled["state"], "settled_failure")
        ao_outcomes.validate_settlement(fixture.directory(), settled)
        delivered = ao_workflow.delivered(fixture.state(), settled["session_id"], fixture.directory())
        self.assertIn(ao_model_boundaries.notice_identity(notices[0]), delivered["notices"])
        after = ao_model_boundaries.read(fixture.directory(), fixture.state(), settled)
        self.assertEqual(after["status"], "frozen")
        self.assertEqual(after["agents"], request["native_worker_expectations"]["agents"])
        self.assertEqual((fixture.directory() / settled["receipt"]).read_bytes(), before)


class QualificationBoundaryReadTests(unittest.TestCase):
    """A real qualified refresh with the same exact worker IDs is still a new identity boundary."""

    def setUp(self):
        import test_ao_routing_qualified as qualified_tests
        self.wrapper = qualified_tests.QualifiedRefreshTests("runTest")
        self.addCleanup(self.wrapper.doCleanups)
        self.wrapper.setUp()
        self.case = self.wrapper.case

    def complete(self, purpose, key):
        case = self.case
        case.fake.snapshots["engineer"]["controller"] = "ready"
        case.send(purpose, key)
        case.fake.finish("engineer", json.dumps(case.report()))
        case.service.ao_room_sync(case.room)

    def test_older_and_newer_requests_keep_their_own_qualification_identity(self):
        case = self.case
        case.service.ao_room_handoff(case.room, str(case.repo))
        artifact = case.qualification_artifact()
        self.complete("implementation", "implementation-initial")
        self.complete("correction", "correction-first")
        older = copy.deepcopy(case.state()["requests"]["correction-first"])
        case.configure_qualification({**copy.deepcopy(artifact), "revision": artifact["revision"] + 1})
        case.fake.snapshots["engineer"]["controller"] = "stopped"
        refreshed = self.wrapper.refresh(agent_selection="qualified")
        self.assertFalse(refreshed["idempotent"])
        self.complete("correction", "correction-second")
        state = case.state()
        newer = state["requests"]["correction-second"]["native_worker_expectations"]
        pointer = state["routing_refresh"]
        self.assertEqual(older["native_worker_expectations"]["qualification_sha256"], qmod.digest(artifact))
        self.assertEqual(newer["qualification_sha256"], refreshed["worker_qualification_sha256"])
        self.assertNotEqual(newer["qualification_sha256"],
                            older["native_worker_expectations"]["qualification_sha256"])
        self.assertEqual(newer["agents"], older["native_worker_expectations"]["agents"])
        historical = ao_model_boundaries.read(case.directory(), state, state["requests"]["correction-first"])
        current = ao_model_boundaries.read(case.directory(), state, state["requests"]["correction-second"])
        self.assertEqual(historical["authority"], {"kind": "preparation"})
        self.assertEqual(historical["qualification_sha256"],
                         older["native_worker_expectations"]["qualification_sha256"])
        self.assertEqual(current["authority"],
                         {"kind": "routing_refresh", "path": pointer["path"], "sha256": pointer["sha256"]})
        self.assertEqual(current["agents"], historical["agents"])
        self.assertEqual(current["qualification_sha256"], newer["qualification_sha256"])


class EqualRoutingAuthorityTests(unittest.TestCase):
    """Two committed ancestors with equal routing bytes keep their own exact authority."""

    def setUp(self):
        import test_ao_routing_refresh as refresh_tests
        self.case = refresh_tests.RoutingRefreshTests("runTest")
        self.addCleanup(self.case.doCleanups)
        self.case.setUp()

    def complete(self, purpose, key):
        case = self.case
        case.fake.snapshots["engineer"]["controller"] = "ready"
        case.send(purpose, key)
        case.fake.finish("engineer", json.dumps(case.report()))
        case.service.ao_room_sync(case.room)

    def test_equal_routing_bytes_keep_disjoint_committed_authorities(self):
        import ao_routing_refresh as refresh
        case = self.case
        case.service.ao_room_handoff(case.room, str(case.repo))
        self.complete("implementation", "implementation-initial")
        case.fake.snapshots["engineer"]["controller"] = "stopped"
        first = case.do_refresh(request_id="refresh-first")
        self.complete("correction", "correction-first")
        case.fake.snapshots["engineer"]["controller"] = "stopped"
        definition = ao_routing.agent_definition
        with mock.patch.object(ao_routing, "agent_definition",
                               side_effect=lambda name, *model: definition(name, *model)
                               + "\nSynthetic intermediate worker instruction.\n"):
            case.do_refresh(request_id="refresh-intermediate")
        case.fake.snapshots["engineer"]["controller"] = "stopped"
        restored = case.do_refresh(request_id="refresh-restored")
        self.complete("correction", "correction-second")
        directory, state = case.directory(), case.state()
        older_record = refresh._read(directory, {"path": first["path"], "sha256": first["sha256"]})
        newer_record = refresh._read(directory, {"path": restored["path"], "sha256": restored["sha256"]})
        self.assertNotEqual((first["path"], first["sha256"]), (restored["path"], restored["sha256"]))
        self.assertEqual(older_record["target"], newer_record["target"])
        older = ao_model_boundaries.read(directory, state, state["requests"]["correction-first"])
        newer = ao_model_boundaries.read(directory, state, state["requests"]["correction-second"])
        self.assertEqual(older["routing"]["sha256"], newer["routing"]["sha256"])
        self.assertEqual(older["authority"],
                         {"kind": "routing_refresh", "path": first["path"], "sha256": first["sha256"]})
        self.assertEqual(newer["authority"],
                         {"kind": "routing_refresh", "path": restored["path"], "sha256": restored["sha256"]})


class BoundExecutableReadinessTests(unittest.TestCase):
    """Readiness resolves the real committed executable binding, not a mocked effective() result."""

    def setUp(self):
        import test_ao_routing_qualified as qualified_tests
        self.wrapper = qualified_tests.QualifiedRefreshTests("runTest")
        self.addCleanup(self.wrapper.doCleanups)
        self.wrapper.setUp()
        self.case = self.wrapper.case

    def test_real_audited_binding_is_resolved_and_a_changed_binary_is_refused(self):
        case = self.case
        replacement = case.root / "replacement-claude"
        replacement.write_text("#!" + sys.executable + "\nimport sys\n"
                               "print('2.1.300 (Claude Code)' if sys.argv[1:] == ['--version'] else 'unexpected')\n")
        replacement.chmod(0o700)
        launch = case.root / "claude-launch"
        launch.symlink_to(replacement)
        case.cli.unlink()
        result = ao_executable_binding.bind(case.service, case.room, "executable-repair", str(replacement),
                                            str(launch), str(case.database),
                                            "Authorized synthetic executable repair",
                                            "Synthetic original binary removed")
        self.assertFalse(result["model_dispatch"])
        self.assertFalse(result["idempotent"])
        self.assertEqual(result["claude"]["path"], str(replacement))
        self.assertEqual(case.state()["executable_binding"],
                         {"path": result["path"], "sha256": result["sha256"]})
        evidence = ao_model_boundaries.readiness(case.directory(), case.state(), "spec_review")
        self.assertEqual(evidence["path"], str(replacement))
        self.assertEqual(evidence["version"], "2.1.300 (Claude Code)")
        replacement.write_text(replacement.read_text() + "\n# synthetic post-binding drift\n")
        with self.assertRaisesRegex(RoomError, "changed"):
            ao_model_boundaries.readiness(case.directory(), case.state(), "spec_review")


if __name__ == "__main__":
    unittest.main()
