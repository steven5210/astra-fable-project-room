"""Independent synthetic dispatch, identity and filesystem regressions for #56."""
import contextlib
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import ao_project_room as ao
import ao_prompt_metrics as metrics
from test_ao_project_room import FakeAO
from test_ao_prompt_metrics import AstraProjectionFixture


def canonical(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


class PhysicalReadBoundTests(unittest.TestCase):
    def test_growth_is_refused_without_reading_beyond_admitted_extent(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "receipt.json"
            path.write_bytes(b"{}")
            original_fdopen = os.fdopen
            observations, changed = [], False

            class Stream:
                def __init__(self, raw):
                    self.raw = raw

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    self.raw.close()

                def fileno(self):
                    return self.raw.fileno()

                def read(self, size=-1):
                    nonlocal changed
                    if not changed:
                        with path.open("ab") as grow:
                            grow.write(b" " * 126)
                        changed = True
                    value = self.raw.read(size)
                    observations.append((len(value), os.lseek(self.fileno(), 0, os.SEEK_CUR)))
                    return value

            def opened(fd, *args, **kwargs):
                return Stream(original_fdopen(fd, *args, **kwargs))

            with patch.object(metrics.os, "fdopen", side_effect=opened):
                with self.assertRaises(metrics.RoomError):
                    metrics._read_bounded_json(path, maximum=2)
            self.assertEqual(observations, [(2, 2)])


class ActualDispatchOracleTests(unittest.TestCase):
    def test_actual_post_and_saved_projection_match_pre_instrumentation_literal_packets(self):
        fixtures = Path(__file__).parent / "tests/fixtures/prompt_projection"
        for case in json.loads((fixtures / "ORACLES.json").read_text())["cases"]:
            with self.subTest(case=case["name"]):
                normal = case["name"] == "normal_reviewer"
                role = "engineer" if case["name"] == "astra_engineer" else "reviewer"
                directory = Path("/synthetic/room")
                state = {"workflow": "fable_engineering" if normal else "astra_led",
                         "spec": "spec.json", "spec_record_sha256": "d" * 64,
                         "checkpoint": "checkpoint.json", "checkpoint_sha256": "c" * 64,
                         "requests": {}, "bindings": {
                             name: {"session_id": name, "model": "astra", "reasoning_effort": "max"}
                             for name in ("engineer", "reviewer")}}
                spec = {"revision": 1, "sha256": "a" * 64, "content": "Δ café.\r\n",
                        "gates": [["python3", "-m", "unittest"]]}
                checkpoint = {"candidate_path": "/synthetic/candidate", "candidate_sha256": "b" * 64}
                fake = FakeAO(Path("/synthetic/candidate"))
                service = object.__new__(ao.Service)
                service.root = Path("/synthetic/home/ao")

                @contextlib.contextmanager
                def locked(room):
                    self.assertEqual(room, "room-1")
                    yield directory, state

                service.locked = locked
                service.quiet = lambda current: None
                service.spec = lambda *args: copy.deepcopy(spec)
                service.client = lambda current: fake
                service.identity = lambda client, current, binding: fake.conversation(binding["session_id"])
                service.checkpoint = lambda *args: copy.deepcopy(checkpoint)
                service.save = lambda *args: None
                with contextlib.ExitStack() as stack:
                    for name in ("ao_acceptance_extension.before_send", "ao_outcomes.gate",
                                 "ao_workflow.ao_delegates.validate_preparation", "ao_workflow.workspace",
                                 "ao_workflow.ao_delegates.assert_settled", "ao_workflow.engineering_ready"):
                        stack.enter_context(patch(name, return_value=None))
                    service.ao_room_send("room-1", role, "Add café.\n", "request-1",
                                         purpose="acceptance_review" if role == "reviewer" else "implementation")
                self.assertEqual(len(fake.posts), 1)
                actual = fake.posts[0][1]["text"].encode()
                self.assertEqual(actual, (fixtures / case["fixture"]).read_bytes())
                projection = state["requests"]["request-1"]["prompt_projection"]
                for field in ("total_bytes", "caller_bytes", "specification_bytes", "workflow_bytes",
                              "separator_bytes", "text_sha256", "spec_delivery"):
                    self.assertEqual(projection[field], case[field], field)


class ReceiptIdentityBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.request = {
            "request_id": "request-1", "role": "engineer", "session_id": "session-1", "harness": "codex",
            "model": "gpt-6-astra", "reasoning_effort": "max", "conversation_id": "conversation-1",
            "branch_id": "conversation-1:root", "baseline": {"conversation_id": "conversation-1",
                "branch_id": "conversation-1:root", "turn_ids": []},
            "created_order": 1, "created_at": 1.0, "state": "completed", "turn_id": "turn-1",
            "provider_turn_id": "native-1", "text": "Continue.",
            "text_sha256": hashlib.sha256(b"Continue.").hexdigest(),
            "acknowledgement": {"turnId": "turn-1"}}
        projection = {"version": 1, "text_sha256": self.request["text_sha256"], "total_bytes": 9,
                      "caller_bytes": 9, "specification_bytes": 0, "workflow_bytes": 0,
                      "separator_bytes": 0, "spec_delivery": "none"}
        self.request["prompt_projection"] = projection
        # Independent canonical preimage, not a call to the production binding helper.
        identity = {"prompt_projection": projection,
                    "request_sha256": hashlib.sha256(b"request-1").hexdigest(),
                    **{key: self.request[key] for key in ("role", "session_id", "harness", "model",
                       "reasoning_effort", "conversation_id", "branch_id", "baseline", "text_sha256",
                       "turn_id", "provider_turn_id")}}
        self.receipt = {"prompt_projection_sha256": canonical(identity),
                        "settings": {"model": "gpt-6-astra", "reasoningEffort": "max"},
                        "turn": {"id": "turn-1", "providerTurnId": "native-1", "state": "completed"},
                        "messages": [{"role": "user", "turnId": "turn-1", "text": "Continue."}],
                        "modelReroute": None, "observed_at": 2.0}
        self.state = {"workflow": "astra_led", "room_id": "room-1", "project_path": "/synthetic/repo",
                      "feature": "synthetic", "ao_url": "http://localhost.invalid", "bindings": {},
                      "verifications": [], "acceptances": [], "requests": {"request-1": self.request}}
        self.write_receipt()

    def write_receipt(self):
        basename = canonical({key: value for key, value in self.receipt.items() if key != "observed_at"})
        self.request["receipt"] = f"receipts/request-1/{basename}.json"
        self.request["receipt_sha256"] = canonical(self.receipt)
        path = self.root / self.request["receipt"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.receipt, ensure_ascii=False, indent=2))
        return path

    def latest(self):
        return metrics.latest_prompt(self.root, self.state)

    def test_independent_binding_and_actual_receipt_have_verified_observed_delivery(self):
        value = self.latest()
        self.assertEqual((value["coverage"], value["integrity"], value["delivery"]),
                         ("known", "receipt_verified", "observed"))
        self.assertEqual(value["reasons"], [])

    def test_saved_owner_mutations_cannot_reuse_the_receipt(self):
        original = copy.deepcopy(self.request)
        for field in ("role", "session_id", "harness", "model", "reasoning_effort", "conversation_id", "branch_id"):
            with self.subTest(field=field):
                self.request.update(copy.deepcopy(original))
                self.request[field] = "reviewer" if field == "role" else "changed"
                value = self.latest()
                self.assertNotEqual(value["integrity"], "receipt_verified")
                self.assertNotEqual(value["delivery"], "observed")
        self.request.update(original)
        self.request["baseline"]["turn_ids"] = ["earlier-turn"]
        self.assertEqual(self.latest()["integrity"], "unavailable")

    def test_canonically_rehashed_receipt_contradictions_remain_unverified(self):
        original = copy.deepcopy(self.receipt)
        for container, field in (("settings", "model"), ("settings", "reasoningEffort"),
                                 ("turn", "id"), ("turn", "providerTurnId")):
            with self.subTest(container=container, field=field):
                self.receipt = copy.deepcopy(original)
                self.receipt[container][field] = "contradiction"
                self.write_receipt()
                value = self.latest()
                self.assertEqual(value["integrity"], "unavailable")
                self.assertEqual(value["delivery"], "submitted")
                self.assertIn("projection_integrity", value["reasons"])

    def test_legacy_native_identity_fallback_retains_honest_delivery_without_projection(self):
        self.request.pop("prompt_projection")
        self.request.pop("provider_turn_id")
        self.request["observed_turn"] = copy.deepcopy(self.receipt["turn"])
        self.receipt.pop("prompt_projection_sha256")
        self.write_receipt()
        value = self.latest()
        self.assertEqual((value["integrity"], value["delivery"]), ("request_observed", "observed"))
        self.assertIsNone(value["caller_bytes"])
        self.assertEqual(value["reasons"], ["legacy_components_absent"])

    def test_malformed_observed_turn_cannot_upgrade_or_break_actual_status(self):
        service = object.__new__(ao.Service)
        for observed in ("invalid", [], 1, False):
            with self.subTest(observed=observed):
                self.request["observed_turn"] = observed
                with patch("ao_reviewer_recovery.summary", return_value=None):
                    value = service.summary(self.root, self.state)["latest_prompt"]
                self.assertEqual(value["coverage"], "known")
                self.assertEqual(value["integrity"], "unavailable")
                self.assertNotEqual(value["delivery"], "observed")

    def test_missing_provider_identity_can_be_bound_without_claiming_observed_delivery(self):
        self.request.pop("provider_turn_id")
        self.request["state"] = "uncertain"
        self.receipt["turn"].pop("providerTurnId")
        self.receipt["turn"]["state"] = "failed"
        self.receipt["prompt_projection_sha256"] = metrics.receipt_projection_sha256(self.request)
        self.write_receipt()
        value = self.latest()
        self.assertEqual((value["integrity"], value["delivery"]), ("receipt_verified", "submitted"))
        self.assertIn("delivery_unobserved", value["reasons"])

    def test_empty_or_malformed_provider_identity_retains_unknown_delivery_receipt(self):
        for provider_id in (None, "", " ", 0, False, [], {}, 1):
            with self.subTest(provider_id=provider_id):
                self.request["provider_turn_id"] = provider_id
                self.request["state"] = "uncertain"
                self.receipt["turn"].update(providerTurnId=provider_id, state="failed")
                self.request["observed_turn"] = copy.deepcopy(self.receipt["turn"])
                self.receipt["prompt_projection_sha256"] = metrics.receipt_projection_sha256(self.request)
                self.write_receipt()
                value = self.latest()
                self.assertEqual((value["coverage"], value["integrity"], value["delivery"]),
                                 ("known", "receipt_verified", "submitted"))
                self.assertEqual(value["reasons"], ["delivery_unobserved"])

    def test_binding_refusal_preserves_known_projection_metrics(self):
        with patch.object(metrics, "projection_binding", side_effect=ao.RoomError("unavailable binding")):
            value = self.latest()
        self.assertEqual((value["coverage"], value["total_bytes"], value["integrity"]),
                         ("known", 9, "unavailable"))
        self.assertEqual(value["reasons"], ["delivery_unobserved", "projection_integrity"])

    def test_missing_receipt_is_unavailable_for_every_controller_terminal_state(self):
        self.assertEqual(set(metrics.TERMINAL_STATES), ao.TERMINAL)
        self.request.pop("receipt")
        self.request.pop("receipt_sha256")
        for terminal in sorted(ao.TERMINAL):
            with self.subTest(state=terminal):
                self.request["state"] = terminal
                value = self.latest()
                self.assertEqual(value["coverage"], "known")
                self.assertEqual(value["reasons"], ["delivery_unobserved", "receipt_unavailable"])

    def test_real_lock_preserves_unproven_state_and_journals_on_projection_refusal(self):
        import ao_history_reconciliation
        self.request["text_sha256"] = "0" * 64
        service = ao.Service(self.root / "service-home")
        directory = service.root / "rooms" / "room-1"
        service.save(directory, self.state)
        before = (directory / "state.json").read_bytes()
        entries = sorted(str(p.relative_to(directory)) for p in directory.rglob("*"))
        service.client = lambda *args: self.fail("AO client constructed despite corrupt projection")
        with patch.object(ao_history_reconciliation, "invalidate_latest",
                          wraps=ao_history_reconciliation.invalidate_latest) as invalidation:
            with self.assertRaisesRegex(ao.RoomError, "saved prompt projection is invalid"):
                service.ao_room_send("room-1", "engineer", "Continue.", "new-request")
        invalidation.assert_called_once()
        self.assertEqual((directory / "state.json").read_bytes(), before)
        self.assertEqual(ao.read(directory / "state.json")["requests"], self.state["requests"])
        self.assertEqual(sorted(str(p.relative_to(directory)) for p in directory.rglob("*")), entries)

    def test_inconsistent_saved_text_hash_refuses_before_dispatch_or_receipt_binding(self):
        self.request['text_sha256'] = '0' * 64
        self.assertEqual(self.latest()['reasons'], ['projection_integrity'])
        with self.assertRaises(ao.RoomError):
            metrics.receipt_projection_sha256(self.request)
        with self.assertRaises(ao.RoomError):
            metrics.projection_binding(self.request, self.request['prompt_projection'])
        service = object.__new__(ao.Service)

        @contextlib.contextmanager
        def locked(room):
            yield self.root, self.state

        service.locked = locked
        service.quiet = lambda *args: self.fail('dispatch admission reached despite corrupt projection')
        service.client = lambda *args: self.fail('AO client constructed despite corrupt projection')
        service.save = lambda *args: self.fail('state rewritten despite corrupt projection')
        before = copy.deepcopy(self.state)
        with self.assertRaises(ao.RoomError):
            service.ao_room_send('room-1', 'engineer', 'Continue.', 'new-request')
        self.assertEqual(self.state, before)

    def test_earlier_order_tie_and_malformed_order_are_unavailable_through_real_summary(self):
        service = object.__new__(ao.Service)
        for orders in ((1, 1, 2), (1, "bad", 2), (1, None, 2), (1, False, 2)):
            with self.subTest(orders=orders):
                self.state["requests"] = {}
                for index, order in enumerate(orders):
                    name = f"request-{index}"
                    request = copy.deepcopy(self.request)
                    request.update(request_id=name, created_order=order)
                    self.state["requests"][name] = request
                with patch("ao_reviewer_recovery.summary", return_value=None):
                    value = service.summary(self.root, self.state)["latest_prompt"]
                self.assertEqual((value["coverage"], value["reasons"]), ("unavailable", ["request_integrity"]))

    def test_traversal_request_identifier_never_reads_a_receipt(self):
        self.request["request_id"] = "../../elsewhere"
        self.state["requests"] = {self.request["request_id"]: self.request}
        with patch.object(metrics, "_read_bounded_json", side_effect=AssertionError("escaped read")):
            self.assertEqual(self.latest()["reasons"], ["request_integrity"])

    def test_replaced_intermediate_directory_cannot_redirect_the_descriptor_read(self):
        directory = self.root / "receipts/request-1"
        previous = self.root / "preserved"
        foreign = self.root / "foreign"
        foreign.mkdir()
        original_open = os.open
        swapped = False

        def opening(path, flags, *args, **kwargs):
            nonlocal swapped
            if isinstance(path, str) and path.endswith(".json") and kwargs.get("dir_fd") is not None:
                self.assertEqual(os.fstat(kwargs["dir_fd"]).st_ino, directory.stat().st_ino)
                directory.rename(previous)
                directory.symlink_to(foreign, target_is_directory=True)
                swapped = True
            return original_open(path, flags, *args, **kwargs)

        with patch.object(metrics.os, "open", side_effect=opening):
            value = self.latest()
        self.assertTrue(swapped)
        self.assertIn("receipt_unavailable", value["reasons"])
        self.assertNotEqual(value["delivery"], "observed")
        self.assertEqual(list(foreign.iterdir()), [])

    def test_populated_container_depth_64_is_accepted_and_65_refused(self):
        self.assertIsInstance(metrics.strict_json("[" * 64 + "0" + "]" * 64), list)
        self.assertIsInstance(metrics.strict_json('{"v":' * 64 + '0' + '}' * 64), dict)
        with self.assertRaises(ao.RoomError):
            metrics.strict_json("[" * 65 + "0" + "]" * 65)


class FailedNativeIdentitySyncTests(AstraProjectionFixture):
    def test_real_sync_preserves_blank_identity_failure_without_observed_delivery(self):
        self.bind()
        self.service.ao_room_send(self.room, "engineer", "Continue.", "request-1")
        self.fake.finish("engineer", state="failed")
        self.fake.snapshots["engineer"]["turns"][-1]["providerTurnId"] = ""
        state = self.state()
        state["requests"]["request-1"]["provider_turn_id"] = ""
        self.service.save(self.directory(), state)
        result = self.service.ao_room_sync(self.room)
        saved = self.state()["requests"]["request-1"]
        self.assertEqual(saved["state"], "uncertain")
        receipt = ao.read(self.directory() / saved["receipt"])
        self.assertEqual(receipt["turn"]["providerTurnId"], "")
        self.assertEqual(receipt["turn"]["state"], "failed")
        self.assertEqual(result["latest_prompt"]["integrity"], "receipt_verified")
        self.assertEqual(result["latest_prompt"]["delivery"], "submitted")
        self.assertIn("delivery_unobserved", result["latest_prompt"]["reasons"])
        self.assertEqual(len(self.fake.posts), 1)


if __name__ == "__main__":
    unittest.main()
