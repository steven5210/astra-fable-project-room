"""W2 synthetic integration regressions: frozen worker expectations and one-time boundary notices.

Composed fixtures only: the normal-room fixture is subclassed for its actual Service send/packet/
receipt paths, while the existing routing-refresh and engineering-transition fixtures are
instantiated (never subclassed) so their unrelated contract tests do not run again. Every boundary
operation calls the real core/refresh function against the in-process fake AO; no AO process,
network, account or model call is made, and no real transition, provider or account is claimed.
"""

import copy
import json
import os
from pathlib import Path
import unittest
from unittest import mock

import ao_delegates
import ao_engineering_model as em
import ao_executable_binding
import ao_model_boundaries
import ao_engineering_transition as et
import ao_project_room as ao
import ao_prompt_metrics
import ao_routing
import ao_workflow
from test_ao_normal import Fixture


class FrozenExpectationTests(Fixture):
    """The actual Service send path: saved intent, projection and post-entry evidence."""

    def setUp(self):
        super().setUp()
        self.room = self.open()
        self.spec()

    def ready_implementation(self):
        self.bind()
        self.agree()
        self.service.ao_room_handoff(self.room, str(self.repo))

    def message_posts(self):
        return [entry for entry in self.fake.posts if entry[0].endswith("/conversation/messages")]

    def rewrite_preparation(self, mutate):
        directory, state = self.directory(), self.state()
        prepared = ao.read(directory / state["preparation"])
        mutate(prepared)
        ao.atomic(directory / state["preparation"], prepared)
        state["preparation_sha256"] = ao.digest(prepared)
        ao.atomic(directory / "state.json", state)

    def historical_family_routing(self, prepared):
        routing = prepared["routing"]
        for name, model in ao_routing.FAMILY_AGENTS.items():
            relative = ".claude/agents/" + name + ".md"
            data = ao_routing.agent_definition(name, model).encode()
            (Path(prepared["worktree"]) / relative).write_bytes(data)
            routing["files"][relative] = ao.digest(data)
        routing["version"] = 2
        routing["agents"] = dict(ao_routing.FAMILY_AGENTS)
        routing["agent_selection"] = {name: dict(item) for name, item in ao_routing.AGENT_SELECTION.items()}
        routing["agent_identity_basis"] = ao_routing.AGENT_IDENTITY_BASIS
        routing.pop("worker_qualification", None)

    def test_frozen_expectation_is_saved_before_the_post_and_reader_resolves_own_epoch(self):
        self.ready_implementation()
        captured = {}
        original = self.fake.request

        def request(method, path, payload=None):
            if method == "POST" and path.endswith("/conversation/messages"):
                captured["state"] = self.state()
                captured["payload"] = copy.deepcopy(payload)
            return original(method, path, payload)

        self.fake.request = request
        self.send("implementation")
        saved = captured["state"]["requests"]["implementation"]
        expectation = saved["native_worker_expectations"]
        self.assertEqual(expectation["agents"], self.QUALIFIED_WORKERS)
        self.assertEqual(expectation["agent_selection"],
                         {"pr-sonnet": {"kind": "family", "family": "sonnet"},
                          "pr-opus": {"kind": "family", "family": "opus"}})
        self.assertEqual(expectation["worker_families"], {"pr-sonnet": "sonnet", "pr-opus": "opus"})
        self.assertEqual(expectation["effort"], "max")
        self.assertEqual(expectation["session_id"], saved["session_id"])
        self.assertEqual(expectation["purpose"], "implementation")
        self.assertEqual(expectation["authority"], {"kind": "preparation"})
        self.assertEqual(expectation["preparation"], self.state()["preparation"])
        self.assertEqual(saved["text"], captured["payload"]["text"])
        self.assertIsNotNone(ao_prompt_metrics.validate_projection(saved["prompt_projection"],
                                                                   captured["payload"]["text"]))
        # An unfinished delivery is never observation authority; the bounded reader refuses the still
        # running request before any receipt exists, and the synthetic owning turn is completed and
        # its receipt observed before the authenticated read below.
        with self.assertRaisesRegex(ao.RoomError, "completed observation authority"):
            ao_model_boundaries.read(self.directory(), self.state(), saved)
        self.fake.finish("engineer", json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        result = ao_model_boundaries.read(self.directory(), self.state(), saved)
        self.assertEqual((result["status"], result["authority"]), ("frozen", {"kind": "preparation"}))
        self.assertEqual(result["agents"], self.QUALIFIED_WORKERS)
        self.assertEqual(result["routing"]["source"], "preparation")

    def test_unqualified_worker_selection_refuses_before_intent_and_post(self):
        self.ready_implementation()
        self.rewrite_preparation(self.historical_family_routing)
        before = (self.directory() / "state.json").read_bytes()
        posts = self.message_posts()
        with self.assertRaisesRegex(ao.RoomError, "audited explicit routing refresh"):
            self.send("implementation")
        self.assertEqual((self.directory() / "state.json").read_bytes(), before)
        self.assertEqual(self.message_posts(), posts)
        self.assertNotIn("implementation", self.state()["requests"])

    def test_missing_selected_worker_refuses_before_intent_and_post(self):
        self.ready_implementation()
        self.rewrite_preparation(lambda prepared: prepared["routing"]["agents"].pop("pr-opus"))
        before = (self.directory() / "state.json").read_bytes()
        posts = self.message_posts()
        with self.assertRaisesRegex(ao.RoomError, "exact map its source qualification derives"):
            self.send("implementation")
        self.assertEqual((self.directory() / "state.json").read_bytes(), before)
        self.assertEqual(self.message_posts(), posts)
        self.assertNotIn("implementation", self.state()["requests"])

    def test_reported_version_drift_refuses_before_intent_with_unchanged_room(self):
        self.ready_implementation()
        prepared = ao.read(self.directory() / self.state()["preparation"])
        recorded = prepared["routing"]["claude"]
        text = self.fixture_claude.read_text()
        self.assertIn("2.1.282", text)
        self.fixture_claude.write_text(text.replace("2.1.282", "2.1.111"))
        os.utime(self.fixture_claude, ns=(recorded["mtime_ns"], recorded["mtime_ns"]))
        self.assertEqual(os.stat(self.fixture_claude).st_size, recorded["size"])
        before = (self.directory() / "state.json").read_bytes()
        posts = self.message_posts()
        with self.assertRaisesRegex(ao.RoomError, "no longer reports the version"):
            self.send("implementation")
        self.assertEqual((self.directory() / "state.json").read_bytes(), before)
        self.assertEqual(self.message_posts(), posts)
        self.assertNotIn("implementation", self.state()["requests"])

    def completed_implementation(self):
        self.ready_implementation()
        self.send("implementation")
        self.fake.finish("engineer", json.dumps(self.report()))
        self.service.ao_room_sync(self.room)

    def test_stale_copy_purpose_and_key_presence_are_refused(self):
        self.completed_implementation()
        directory, state = self.directory(), self.state()
        saved = state["requests"]["implementation"]
        stale = copy.deepcopy(saved)
        stale["native_worker_expectations"]["routing_sha256"] = "0" * 64
        with self.assertRaisesRegex(ao.RoomError, "retained record"):
            ao_model_boundaries.read(directory, state, stale)
        purpose = copy.deepcopy(saved)
        purpose["purpose"] = "spec_review"
        with self.assertRaisesRegex(ao.RoomError, "retained record"):
            ao_model_boundaries.read(directory, state, purpose)
        deleted = copy.deepcopy(saved)
        deleted.pop("native_worker_expectations")
        with self.assertRaisesRegex(ao.RoomError, "retained record"):
            ao_model_boundaries.read(directory, state, deleted)

    def test_deleting_a_bound_field_is_never_authentic_absence(self):
        self.completed_implementation()
        directory, state = self.directory(), self.state()
        request = state["requests"]["implementation"]
        receipt = (directory / request["receipt"]).read_bytes()
        with self.service.locked(self.room) as (locked_directory, locked):
            locked["requests"]["implementation"].pop("native_worker_expectations")
            self.service.save(locked_directory, locked)
        with self.assertRaisesRegex(ao.RoomError, "completed observation authority"):
            ao_model_boundaries.read(directory, self.state(), self.state()["requests"]["implementation"])
        self.assertEqual((directory / self.state()["requests"]["implementation"]["receipt"]).read_bytes(), receipt)

    def test_genuinely_absent_field_request_receipt_pair_stays_readable(self):
        self.bind()
        self.rewrite_preparation(lambda prepared: prepared.pop("routing"))
        self.agree()
        request = self.state()["requests"]["spec_review"]
        self.assertNotIn("native_worker_expectations", request)
        result = ao_model_boundaries.read(self.directory(), self.state(), request)
        self.assertEqual((result["status"], result["reason"]),
                         ("absent", "no_frozen_worker_expectations"))
        nulled = copy.deepcopy(request)
        nulled["native_worker_expectations"] = None
        with self.assertRaisesRegex(ao.RoomError, "retained record"):
            ao_model_boundaries.read(self.directory(), self.state(), nulled)

    def test_explicit_null_empty_or_malformed_expectation_is_an_error(self):
        self.completed_implementation()
        for broken in (None, {}, {"version": 1}, "implementation"):
            with self.subTest(broken=broken):
                with self.service.locked(self.room) as (directory, state):
                    state["requests"]["implementation"]["native_worker_expectations"] = copy.deepcopy(broken)
                    self.service.save(directory, state)
                with self.assertRaisesRegex(ao.RoomError, "present but empty, malformed or unsupported"):
                    ao_model_boundaries.read(self.directory(), self.state(),
                                             self.state()["requests"]["implementation"])

    def test_forged_nondelegating_purpose_refuses(self):
        self.completed_implementation()
        with self.service.locked(self.room) as (directory, state):
            state["requests"]["implementation"]["purpose"] = "spec_review"
            self.service.save(directory, state)
        with self.assertRaisesRegex(ao.RoomError, "delegation-capable"):
            ao_model_boundaries.read(self.directory(), self.state(),
                                     self.state()["requests"]["implementation"])

    def test_forged_value_purpose_refuses(self):
        self.completed_implementation()
        with self.service.locked(self.room) as (directory, state):
            state["requests"]["implementation"]["native_worker_expectations"]["purpose"] = "acceptance_review"
            self.service.save(directory, state)
        with self.assertRaisesRegex(ao.RoomError, "delegation-capable"):
            ao_model_boundaries.read(self.directory(), self.state(),
                                     self.state()["requests"]["implementation"])

    def test_uncertain_notice_delivery_is_held_and_never_silently_retried(self):
        self.bind()
        self.agree()
        self.service.ao_room_handoff(self.room, str(self.repo))
        baseline = len(self.message_posts())  # the initial charter review already posted once
        self.fake.lose_ack = True
        first = self.send("implementation")
        self.assertEqual(first["state"], "uncertain")
        self.assertEqual(len(self.message_posts()), baseline + 1)  # exactly one uncertain submission
        saved = self.state()["requests"]["implementation"]
        self.assertTrue(saved["carried"]["boundary_notices"])
        with self.assertRaisesRegex(ao.RoomError, "active or uncertain"):
            self.send("correction")
        self.assertEqual(len(self.message_posts()), baseline + 1)  # the held correction adds none
        self.fake.lose_ack = False
        self.fake.finish("engineer", json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        self.send("correction")
        correction = self.state()["requests"]["correction"]
        self.assertNotIn("boundary_notices", correction.get("carried") or {})
        self.assertEqual(correction["text"], "Perform the exact authorized purpose.")


    def test_raised_root_floor_refuses_before_intent_and_post(self):
        self.ready_implementation()
        directory = self.directory()
        before = (directory / "state.json").read_bytes()
        posts = self.message_posts()
        with mock.patch.dict(em.BUNDLED_FAMILIES["fable"], {"minimum_claude_code_version": "2.1.999"}):
            with self.assertRaisesRegex(ao.RoomError, "2.1.999 or newer"):
                self.send("implementation")
        self.assertEqual((directory / "state.json").read_bytes(), before)
        self.assertEqual(self.message_posts(), posts)
        self.assertNotIn("implementation", self.state()["requests"])

    def test_spec_review_reported_version_drift_refuses_before_post(self):
        self.bind()
        self.agree()
        directory, state = self.directory(), self.state()
        recorded = ao.read(directory / state["preparation"])["routing"]["claude"]
        text = self.fixture_claude.read_text()
        self.assertIn("2.1.282", text)
        self.fixture_claude.write_text(text.replace("2.1.282", "2.1.111"))
        os.utime(self.fixture_claude, ns=(recorded["mtime_ns"], recorded["mtime_ns"]))
        self.assertEqual(os.stat(self.fixture_claude).st_size, recorded["size"])
        posts = self.message_posts()
        with self.assertRaisesRegex(ao.RoomError, "no longer reports the version"):
            self.send("spec_review", "second-charter")
        self.assertEqual(self.message_posts(), posts)
        self.assertNotIn("second-charter", self.state()["requests"])

    def test_readiness_reuses_the_audited_executable_binding_lane(self):
        self.ready_implementation()
        directory, state = self.directory(), self.state()
        recorded = ao.read(directory / state["preparation"])["routing"]["claude"]
        second = self.root / "fixture-claude-second"
        second.write_text(self.fixture_claude.read_text())
        second.chmod(0o700)
        evidence = ao_routing.claude_evidence(str(second))
        with mock.patch.object(ao_executable_binding, "effective", return_value=evidence):
            self.assertEqual(ao_model_boundaries.readiness(directory, state, "spec_review"), evidence)
        missing = {**recorded, "path": str(self.root / "missing-rebound-claude"), "size": 1, "mtime_ns": 1}
        posts = self.message_posts()
        with mock.patch.object(ao_executable_binding, "effective", return_value=missing):
            with self.assertRaisesRegex(ao.RoomError, "changed or became unreadable"):
                self.send("implementation")
        self.assertEqual(self.message_posts(), posts)
        self.assertNotIn("implementation", self.state()["requests"])

    def test_readiness_refusal_keeps_the_existing_diagnostic_invalidation_path(self):
        self.ready_implementation()
        state = self.state()
        requests = set(state["requests"])
        posts = self.message_posts()
        with mock.patch.dict(em.BUNDLED_FAMILIES["fable"], {"minimum_claude_code_version": "2.1.999"}):
            with mock.patch("ao_history_reconciliation.invalidate_latest") as invalidate:
                with self.assertRaisesRegex(ao.RoomError, "2.1.999 or newer"):
                    self.send("implementation")
        self.assertEqual(invalidate.call_count, 1)
        self.assertEqual(set(self.state()["requests"]), requests)
        self.assertEqual(self.message_posts(), posts)


class RefreshBoundaryTests(unittest.TestCase):
    """One real qualified refresh between two actual sends, composed from the refresh fixture."""

    def setUp(self):
        import test_ao_routing_refresh as refresh_tests
        self.case = refresh_tests.RoutingRefreshTests("runTest")
        self.addCleanup(self.case.doCleanups)
        self.case.setUp()

    def message_posts(self):
        return [entry for entry in self.case.fake.posts if entry[0].endswith("/conversation/messages")]

    def test_refresh_keeps_the_old_request_and_delivers_one_new_worker_notice(self):
        case = self.case
        case.service.ao_room_handoff(case.room, str(case.repo))
        case.send("implementation")
        case.fake.finish("engineer", json.dumps(case.report()))
        case.service.ao_room_sync(case.room)
        saved = copy.deepcopy(case.state()["requests"]["implementation"])
        before = len(self.message_posts())
        case.do_refresh()
        self.assertEqual(len(self.message_posts()), before)  # boundary creation sends no model message
        case.fake.snapshots["engineer"]["controller"] = "ready"
        posted = {}
        original_request = case.fake.request

        def request(method, path, payload=None):
            if method == "POST" and path.endswith("/conversation/messages"):
                posted["request"] = copy.deepcopy(case.state()["requests"]["correction"])
            return original_request(method, path, payload)

        case.fake.request = request
        try:
            case.send("correction")
        finally:
            case.fake.request = original_request
        # The frozen expectation and its refreshed notice are durable before the POST; the strict
        # reader authenticates them only against this request's own persisted receipt, so the real
        # synthetic owning turn is completed before the reads below.
        self.assertEqual(posted["request"]["native_worker_expectations"],
                         case.state()["requests"]["correction"]["native_worker_expectations"])
        case.fake.finish("engineer", json.dumps(case.report()))
        case.service.ao_room_sync(case.room)
        state = case.state()
        self.assertEqual(state["requests"]["implementation"]["native_worker_expectations"],
                         saved["native_worker_expectations"])
        self.assertEqual(state["requests"]["implementation"]["text"], saved["text"])
        self.assertEqual(state["requests"]["implementation"]["prompt_projection"], saved["prompt_projection"])
        historical = ao_model_boundaries.read(case.directory(), state, state["requests"]["implementation"])
        self.assertEqual(historical["authority"], {"kind": "preparation"})
        self.assertEqual(historical["routing"]["source"], "preparation")
        pointer = state["routing_refresh"]
        fresh = ao_model_boundaries.read(case.directory(), state, state["requests"]["correction"])
        self.assertEqual(fresh["authority"],
                         {"kind": "routing_refresh", "path": pointer["path"], "sha256": pointer["sha256"]})
        self.assertEqual(fresh["agents"], case.QUALIFIED_WORKERS)
        initial = saved["carried"]["boundary_notices"]
        self.assertEqual((len(initial), initial[0]["authority"]["kind"]), (1, "preparation"))
        refreshed = state["requests"]["correction"]["carried"]["boundary_notices"]
        self.assertEqual(len(refreshed), 1)
        self.assertEqual(refreshed[0]["authority"]["kind"], "routing_refresh")
        self.assertNotEqual(refreshed[0]["authority"], initial[0]["authority"])
        self.assertEqual(refreshed[0]["fragment_sha256"],
                         ao_model_boundaries.digest(
                             ao_model_boundaries.notice_fragment(case.directory(), state,
                                                                 refreshed[0]).encode()))
        with case.service.locked(case.room) as (directory, locked):
            locked["requests"]["correction"]["native_worker_expectations"] = copy.deepcopy(
                saved["native_worker_expectations"])
            case.service.save(directory, locked)
        status = case.service.ao_room_status(case.room)
        self.assertEqual(status["latest_prompt"]["integrity"], "unavailable")
        self.assertIn("projection_integrity", status["latest_prompt"]["reasons"])

    def test_same_purpose_older_authority_and_deleted_field_never_read_as_authenticated(self):
        case = self.case
        case.service.ao_room_handoff(case.room, str(case.repo))
        case.send("implementation")
        case.fake.finish("engineer", json.dumps(case.report()))
        case.service.ao_room_sync(case.room)
        case.fake.snapshots["engineer"]["controller"] = "ready"
        case.send("correction", "correction-1")
        case.fake.finish("engineer", json.dumps(case.report()))
        case.service.ao_room_sync(case.room)
        case.fake.snapshots["engineer"]["controller"] = "stopped"
        case.do_refresh()
        case.fake.snapshots["engineer"]["controller"] = "ready"
        case.send("correction", "correction-2")
        case.fake.finish("engineer", json.dumps(case.report()))
        case.service.ao_room_sync(case.room)
        directory, state = case.directory(), case.state()
        earlier = copy.deepcopy(state["requests"]["correction-1"]["native_worker_expectations"])
        later = copy.deepcopy(state["requests"]["correction-2"]["native_worker_expectations"])
        self.assertEqual(earlier["purpose"], later["purpose"])
        self.assertNotEqual(earlier["authority"], later["authority"])
        untouched = ao_model_boundaries.read(directory, state, state["requests"]["correction-2"])
        pointer = state["routing_refresh"]
        self.assertEqual((untouched["status"], untouched["authority"]),
                         ("frozen", {"kind": "routing_refresh", "path": pointer["path"],
                                     "sha256": pointer["sha256"]}))
        receipt_path = directory / state["requests"]["correction-2"]["receipt"]
        receipt = receipt_path.read_bytes()
        with case.service.locked(case.room) as (locked_directory, locked):
            locked["requests"]["correction-2"]["native_worker_expectations"] = copy.deepcopy(earlier)
            case.service.save(locked_directory, locked)
        with self.assertRaisesRegex(ao.RoomError, "completed observation authority"):
            ao_model_boundaries.read(directory, case.state(), case.state()["requests"]["correction-2"])
        self.assertEqual(receipt_path.read_bytes(), receipt)
        with case.service.locked(case.room) as (locked_directory, locked):
            locked["requests"]["correction-2"].pop("native_worker_expectations")
            case.service.save(locked_directory, locked)
        with self.assertRaisesRegex(ao.RoomError, "completed observation authority"):
            ao_model_boundaries.read(directory, case.state(), case.state()["requests"]["correction-2"])
        self.assertEqual(receipt_path.read_bytes(), receipt)


class RootNoticeSemanticsTests(Fixture):
    """Notice construction over the actual authenticated root qualification fixture.

    The source-qualified case is the real root epoch of a fixture room, whose retained qualification
    snapshot carries its own selected source descriptors; the exact, unresolved and
    observed-resolution cases are the documented historical epoch shapes. No source guard is relaxed.
    """

    def setUp(self):
        super().setUp()
        self.room = self.open()
        self.spec()
        self.qualified = em.current(self.directory(), self.state())

    def boundary(self, kind, epoch):
        return {"kind": kind, "authority": {"kind": "root_model_change", "request_id": "r",
                                            "record_sha256": "a" * 64}, "epoch": epoch,
                "reset": kind == ao_model_boundaries.ROOT_RESET}

    def test_exact_source_qualified_resolved_and_unresolved_expectations_are_distinct(self):
        change = self.boundary(ao_model_boundaries.ROOT_CHANGE, None)
        exact = em._epoch({"kind": "exact", "model": "claude-opus-5-5"},
                          {"harness": "claude-code", "reasoning_effort": "max",
                           "minimum_claude_code_version": "2.1.280"}, 1)
        exact_text = ao_model_boundaries.fragment_text({**change, "epoch": exact})
        self.assertIn("configured exact expected model is claude-opus-5-5", exact_text)
        self.assertNotIn("The source-qualified exact expected model", exact_text)
        unresolved = em._epoch({"kind": "family", "family": "fable"},
                               {"harness": "claude-code", "reasoning_effort": "max"}, 1)
        unresolved_text = ao_model_boundaries.fragment_text({**change, "epoch": unresolved})
        self.assertIn("No pre-inference source qualification and no completed owned turn establish",
                      unresolved_text)
        self.assertNotIn("The source-qualified exact expected model", unresolved_text)
        resolved = em._epoch({"kind": "family", "family": "fable"},
                             {"harness": "claude-code", "reasoning_effort": "max"}, 1)
        resolved.update(expected_model="claude-fable-5-2", resolution_sha256="c" * 64, resolution_order=1)
        resolved_text = ao_model_boundaries.fragment_text({**change, "epoch": resolved})
        self.assertIn("observed on a completed owned turn", resolved_text)
        self.assertNotIn("The source-qualified exact expected model", resolved_text)
        # The source-qualified case uses the room's own authenticated qualification snapshot,
        # including its selected source descriptors, instead of a digest-only placeholder.
        self.assertTrue(self.qualified["family_qualified"])
        self.assertEqual(self.qualified["expected_model"], self.QUALIFIED_MODEL)
        reset = self.boundary(ao_model_boundaries.ROOT_RESET, self.qualified)
        qualified_text = ao_model_boundaries.fragment_text(reset)
        self.assertIn("source-qualified exact expected model is " + self.qualified["expected_model"],
                      qualified_text)
        self.assertNotIn("unresolved", qualified_text)
        plain_reset = self.boundary(ao_model_boundaries.ROOT_RESET, unresolved)
        self.assertIn("starts unresolved again",
                      ao_model_boundaries.fragment_text({**change, **plain_reset}))


class TransitionBoundaryTests(unittest.TestCase):
    """One real committed root transition plus refresh, disclosed once on the next send."""

    def setUp(self):
        import test_ao_engineering_transition_refresh as transition_tests
        self.case = transition_tests.TransitionAfterRealRefreshTests("runTest")
        self.addCleanup(self.case.doCleanups)
        self.case.setUp()

    def message_posts(self):
        return [entry for entry in self.case.fake.posts if entry[0].endswith("/conversation/messages")]

    def test_combined_root_and_worker_notices_are_delivered_exactly_once(self):
        case = self.case
        before = len(self.message_posts())
        result = et.transition(case.service, **case.arguments("model-refreshed"))
        self.assertTrue(result["transitioned"])
        self.assertEqual(len(self.message_posts()), before)  # a boundary alone wakes no model
        self.assertTrue(case.patches)
        case.service.ao_room_handoff(case.room, str(case.repo))
        case.send("implementation")
        state = case.state()
        saved = state["requests"]["implementation"]
        notices = saved["carried"]["boundary_notices"]
        self.assertEqual([notice["kind"] for notice in notices],
                         [ao_model_boundaries.ROOT_CHANGE, ao_model_boundaries.WORKER_ROUTING])
        fragments = []
        for notice in notices:
            fragment = ao_model_boundaries.notice_fragment(case.directory(), state, notice)
            fragments.append(fragment)
            self.assertEqual(saved["text"].count(fragment), 1)
        self.assertNotIn("Implement the exact test contract.", saved["text"])
        case.fake.finish("engineer", json.dumps(case.report()))
        case.service.ao_room_sync(case.room)
        case.send("correction")
        state = case.state()
        correction = state["requests"]["correction"]
        self.assertNotIn("boundary_notices", correction.get("carried") or {})
        self.assertEqual(correction["text"], "Perform the exact authorized purpose.")
        for fragment in fragments:
            self.assertNotIn(fragment, correction["text"])


class PreCharterRefreshFramingTests(Fixture):
    """One real committed refresh before the session's first engineer packet (C2 F1).

    The base normal fixture is used directly and the two supplied AdoptionFixture SQL owner/source
    helpers are composed by name, so that suite's unrelated provider/adoption tests never run. The
    refresh, the Service send, the packet assembly, the saved projection and the later worker notice
    are the real implementations; only the AO transport and the synthetic owner rows are fixtures.
    """

    def make_database(self):
        from test_ao_adoption import AdoptionFixture
        return AdoptionFixture.make_database(self)

    def register_native_source(self):
        from test_ao_adoption import AdoptionFixture
        return AdoptionFixture.register_native_source(self)

    def message_posts(self):
        return [entry for entry in self.fake.posts if entry[0].endswith("/conversation/messages")]

    def setUp(self):
        super().setUp()
        import ao_routing_refresh as refresh
        from test_ao_routing_refresh import historical_routing
        self.refresh = refresh
        self.room = self.open()
        self.spec()
        self.service.ao_room_prepare(self.room, str(self.repo))
        historical_routing(self, ao_routing.FAMILY_AGENTS, ao_routing.AGENT_SELECTION,
                           ao_routing.AGENT_IDENTITY_BASIS)
        self.bind()
        self.original = ao_delegates.preparation(self.directory(), self.state())
        self.preparation_path = self.directory() / self.state()['preparation']
        self.preparation_raw = self.preparation_path.read_bytes()
        self.assertEqual(self.original['routing']['version'], 2)
        for session in self.fake.sessions.values():
            session['isTerminated'] = False
        self.fake.snapshots['engineer'].update(controller='stopped', hasMoreBefore=False, activities=[],
            branchMaterialization={'strategy': 'native', 'replayTruncated': False})
        original_request = self.fake.request
        self.posted = {}

        def transport(method, path, payload=None):
            if method == 'GET' and '/conversation?' in path:
                return copy.deepcopy(self.fake.snapshots[path.split('/')[2]])
            if method == 'POST' and path.endswith('/conversation/messages'):
                self.posted['payload'] = copy.deepcopy(payload)
            return original_request(method, path, payload)

        self.fake.request = transport
        self.refresh_result = refresh.refresh(
            self.service, self.room, 'before-first-packet', str(self.database), self.NATIVE,
            'User authorized this offline synthetic source-qualified worker refresh.',
            'Upgrade the historical family worker configuration before the first authorized engineer request.',
            agent_selection='qualified')

    def test_committed_refresh_frames_the_frozen_routing_part_before_first_delivery(self):
        self.assertEqual(self.state()['requests'], {})
        self.assertEqual(self.message_posts(), [])
        self.assertFalse(self.refresh_result['model_dispatch'])
        record = self.refresh._read(self.directory(), self.state()['routing_refresh'])
        self.assertEqual(record['target']['version'], 3)
        self.assertEqual(record['target']['agents'], self.QUALIFIED_WORKERS)
        self.assertEqual(record['evidence']['native']['turn_count'], 0)
        policy = ao_delegates.validate_provider(self.directory(), self.state())
        texts = ao_workflow.part_texts(self.original, policy)
        part = texts['routing']
        self.assertIn('family alias sonnet', part)
        self.assertIn('family alias opus', part)
        self.fake.snapshots['engineer']['controller'] = 'ready'
        self.send('spec_review', 'first-spec-review')
        state = self.state()
        request = state['requests']['first-spec-review']
        sent = self.posted['payload']['text']
        self.assertEqual(request['text'], sent)
        self.assertEqual(request['state'], 'submitted')
        self.assertEqual(len(self.message_posts()), 1)
        self.assertEqual(request['carried']['part_sha256']['routing'], ao.digest(part.encode()))
        self.assertEqual(sent.count(part), 1)
        framing = ao_workflow.superseded_routing_framing(self.directory(), state, self.original)
        self.assertIsInstance(framing, str)
        self.assertIn('historical', framing)
        self.assertIn('superseded', framing)
        self.assertIn('read-only', framing)
        self.assertEqual(sent.count(framing), 1)
        self.assertLess(sent.index(part), sent.index(framing))
        self.assertNotIn('boundary_notices', request['carried'])
        self.assertNotIn('native_worker_expectations', request)
        for model in record['target']['agents'].values():
            self.assertNotIn(model, sent)
        projection = request['prompt_projection']
        self.assertEqual(projection['caller_bytes'], len('Perform the exact authorized purpose.'.encode()))
        self.assertEqual(projection['spec_delivery'], 'full')
        self.assertEqual(projection['total_bytes'], len(sent.encode()))
        self.assertEqual(projection['text_sha256'], ao.digest(sent.encode()))
        self.assertIsNotNone(ao_prompt_metrics.validate_projection(projection, sent))
        spec = self.service.spec(self.directory(), state)
        block = ('Exact specification revision ' + str(spec['revision']) + ', SHA256 ' + spec['sha256']
                 + '\n<specification>\n' + spec['content'] + '\n</specification>\nAgreed gates: '
                 + json.dumps(spec['gates']))
        from ao_report_contract import PART, INSTRUCTION, DELEGATION_PART, DELEGATION_INSTRUCTION
        from ao_review_followups import PART as FOLLOWUPS_PART, INSTRUCTION as FOLLOWUPS_INSTRUCTION
        import ao_progress
        import ao_read_admission
        texts[PART] = INSTRUCTION
        texts[DELEGATION_PART] = DELEGATION_INSTRUCTION
        texts[FOLLOWUPS_PART] = FOLLOWUPS_INSTRUCTION
        texts[ao_progress.PART] = ao_progress.INSTRUCTION
        texts[ao_progress.PART_V2] = ao_progress.INSTRUCTION_V2
        texts[ao_read_admission.PART] = ao_read_admission.INSTRUCTION
        import ao_residual_escalation
        texts[ao_residual_escalation.PART] = ao_residual_escalation.INSTRUCTION
        import lifecycle_closure
        texts[lifecycle_closure.PART] = lifecycle_closure.INSTRUCTION
        assembly = ao_prompt_metrics.PromptAssembly('\n')
        for name in request['carried']['parts']:
            assembly.add('workflow', texts[name])
            if name == 'routing':
                assembly.add('workflow', framing)
        assembly.add('specification', block)
        assembly.add('caller', 'Perform the exact authorized purpose.')
        self.assertEqual(assembly.text, sent)
        self.assertEqual(assembly.projection('full', sent), projection)
        self.assertEqual(self.preparation_path.read_bytes(), self.preparation_raw)
        self.fake.finish('engineer', json.dumps({'interpretation': 'The exact specification is implementable.',
                                                 'findings': [], 'decision': 'accept',
                                                 'spec_revision': spec['revision'],
                                                 'spec_sha256': spec['sha256']}))
        self.service.ao_room_sync(self.room)
        self.assertEqual(self.state()['requests']['first-spec-review']['state'], 'completed')
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.send('implementation', 'first-implementation')
        state = self.state()
        implementation = state['requests']['first-implementation']
        sent = self.posted['payload']['text']
        self.assertEqual(implementation['text'], sent)
        notices = implementation['carried']['boundary_notices']
        self.assertEqual([notice['kind'] for notice in notices], [ao_model_boundaries.WORKER_ROUTING])
        worker_text = ao_model_boundaries.notice_fragment(self.directory(), state, notices[0])
        self.assertEqual(sent.count(worker_text), 1)
        for model in record['target']['agents'].values():
            self.assertIn(model, worker_text)
        self.assertIn('MAX', worker_text)
        self.assertEqual(implementation['carried']['parts'], [])
        self.assertNotIn(part, sent)
        self.assertNotIn(framing, sent)
        self.assertNotIn('<specification>', sent)
        self.assertEqual(implementation['prompt_projection']['specification_bytes'], 0)
        self.assertTrue(sent.endswith('Perform the exact authorized purpose.'))
        self.fake.finish('engineer', json.dumps(self.report(outcome='changes_required', implementation_complete=False)))
        self.service.ao_room_sync(self.room)
        self.send('correction', 'first-correction')
        correction = self.state()['requests']['first-correction']
        self.assertEqual(correction['text'], 'Perform the exact authorized purpose.')
        self.assertNotIn('boundary_notices', correction.get('carried') or {})
        self.assertNotIn(part, correction['text'])
        self.assertNotIn(framing, correction['text'])
        self.assertEqual(self.preparation_path.read_bytes(), self.preparation_raw)


if __name__ == "__main__":
    unittest.main()
