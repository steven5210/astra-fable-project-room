"""History transport and workflow regressions. Synthetic data, no sockets or models."""

import copy
import io
import json
import unittest
import urllib.error
import urllib.parse
from unittest.mock import patch

import ao_history as history
import ao_project_room as ao
from ao_provider_transition import _CompleteClient
from test_ao_normal import Fixture


class HistoryTests(unittest.TestCase):
    def pages(self):
        identity = {"sessionId": "fixture", "conversationId": "native", "activeBranchId": "native:root",
                    "controller": "ready", "settings": {"model": "claude-fable-5-1", "reasoningEffort": "max"},
                    "branchMaterialization": {"strategy": "native"}}
        return [
            {**copy.deepcopy(identity), "turns": [{"id": "t2", "state": "completed"}],
             "messages": [{"id": "m2", "sequence": 20}], "activities": [{"id": "a2", "sequence": 21}],
             "hasMoreBefore": True, "oldestSequence": 20},
            {**copy.deepcopy(identity), "turns": [{"id": "t1", "state": "completed"}],
             "messages": [{"id": "m1", "sequence": 1}],
             "activities": [{"id": "a1", "sequence": 2, "status": "failed", "summary": "synthetic quota error"}],
             "hasMoreBefore": False, "oldestSequence": 1},
        ]

    def observe(self, pages, strict=True):
        client = ao.Client("http://127.0.0.1:1")
        with patch.object(client, "request", side_effect=copy.deepcopy(pages)) as raw:
            result = (_CompleteClient(client) if strict else client).conversation("fixture")
        return raw, result

    def test_all_history_readers_preserve_older_failure_activity(self):
        pages = self.pages()
        for strict in (False, True):
            with self.subTest(strict=strict):
                raw, result = self.observe(pages, strict)
                self.assertEqual(raw.call_args_list[0].args[:2], ("GET", "/sessions/fixture/conversation?limit=100"))
                self.assertIn("beforeSequence=20", raw.call_args_list[1].args[1])
                self.assertEqual([m["id"] for m in result["messages"]], ["m1", "m2"])
                self.assertEqual(len(result["turns"]), 2)
                self.assertEqual({a["id"] for a in result["activities"]}, {"a1", "a2"})
                self.assertIn(pages[1]["activities"][0], result["activities"])
                self.assertFalse(result["history_truncated"])
                self.assertFalse(result["hasMoreBefore"])

    def test_actual_oversized_response_shrinks_read_pages_without_losing_evidence(self):
        # A native timeline larger than the transport cap, with a complete reference.
        entries = [{"id": "item-" + str(i), "sequence": i, "text": "x" * 81_000} for i in range(1, 306)]
        entries[0]["status"] = "failed"
        base = self.pages()[0]
        client = ao.Client("http://127.0.0.1:1")
        queries = []
        def open_response(request, timeout):
            self.assertEqual(request.get_method(), "GET")
            self.assertIsNone(request.data)
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
            queries.append(query)
            limit = int(query["limit"][0])
            before = int(query.get("beforeSequence", [306])[0])
            candidates = [item for item in entries if item["sequence"] < before]
            items = candidates[-limit:]
            page = {**base, "messages": [x for x in items if x["sequence"] % 2 == 0],
                    "activities": [x for x in items if x["sequence"] % 2],
                    "hasMoreBefore": len(candidates) > len(items), "oldestSequence": items[0]["sequence"]}
            return io.BytesIO(json.dumps(page).encode())
        with patch.object(client.opener, "open", side_effect=open_response):
            result = _CompleteClient(client).conversation("fixture")
        actual = sorted(result["messages"] + result["activities"], key=lambda x: x["sequence"])
        self.assertEqual(actual, entries)
        self.assertEqual(queries[0], {"limit": ["100"]})
        self.assertEqual(queries[1], {"limit": ["50"]})
        self.assertEqual(len(queries), 8)
        self.assertFalse(result["history_truncated"])

    def test_oversized_older_page_repeats_only_its_cursor(self):
        client = ao.Client("http://127.0.0.1:1")
        pages = self.pages()
        with patch.object(client, "request", side_effect=[pages[0], history.ResponseTooLarge(), pages[1]]) as raw:
            result = _CompleteClient(client).conversation("fixture")
        self.assertEqual([c.args[1] for c in raw.call_args_list], [
            "/sessions/fixture/conversation?limit=100",
            "/sessions/fixture/conversation?limit=100&beforeSequence=20",
            "/sessions/fixture/conversation?limit=50&beforeSequence=20"])
        self.assertEqual(len(result["activities"]), 2)

    def test_single_oversized_item_stops_with_no_model_retry(self):
        client = ao.Client("http://127.0.0.1:1")
        with patch.object(client.opener, "open", side_effect=lambda *a, **k: io.BytesIO(b" " * 8_000_001)) as raw:
            with self.assertRaisesRegex(ao.RoomError, "One AO history item exceeds"):
                client.conversation("fixture")
        self.assertEqual(raw.call_count, 7)
        self.assertTrue(all(c.args[0].get_method() == "GET" and c.args[0].data is None for c in raw.call_args_list))

    def test_write_oversize_and_network_errors_are_never_retried(self):
        for method, path, payload in [("POST", "/sessions/fixture/conversation/messages", {"text": "Synthetic"}),
                                      ("GET", "/sessions/fixture", None)]:
            client = ao.Client("http://127.0.0.1:1")
            with patch.object(client.opener, "open", return_value=io.BytesIO(b" " * 8_000_001)) as raw:
                with self.assertRaises(history.ResponseTooLarge):
                    client.request(method, path, payload)
                self.assertEqual(raw.call_count, 1)
        for error in (urllib.error.URLError("synthetic disconnect"), ao.RoomError("response exceeds 8 MB, but not typed")):
            client = ao.Client("http://127.0.0.1:1")
            with patch.object(client.opener, "open", side_effect=error) as raw:
                with self.assertRaises(ao.RoomError):
                    client.conversation("fixture")
                self.assertEqual(raw.call_count, 1)

    def test_missing_arrays_and_duplicate_identities_refuse_in_both_readers(self):
        for strict in (False, True):
            for name in (*history.COLLECTIONS, "hasMoreBefore"):
                pages = self.pages(); del pages[1][name]
                with self.subTest(strict=strict, missing=name), self.assertRaises(ao.RoomError):
                    self.observe(pages, strict)
            for name in history.COLLECTIONS:
                pages = self.pages(); pages[1][name] *= 2
                with self.subTest(strict=strict, duplicate=name), self.assertRaisesRegex(ao.RoomError, "duplicate identities"):
                    self.observe(pages, strict)

    def test_activity_conflicts_are_rejected_by_complete_audit(self):
        pages = self.pages()
        pages[1]["activities"].append({**pages[0]["activities"][0], "status": "failed"})
        with self.assertRaisesRegex(ao.RoomError, "conflicting activities"):
            self.observe(pages)

    def test_invalid_cursors_and_contradictory_truncation_refuse(self):
        for cursor in (True, 0, -1, "20", 20, 21):
            pages = self.pages(); pages[1].update(hasMoreBefore=True, oldestSequence=cursor)
            with self.subTest(cursor=cursor), self.assertRaisesRegex(ao.RoomError, "pagination is incomplete"):
                self.observe(pages)
        pages = self.pages(); pages[1]["history_truncated"] = True
        with self.assertRaisesRegex(ao.RoomError, "contradictory truncation"):
            self.observe(pages)

    def test_identity_changes_refuse_in_ordinary_reader_too(self):
        pages = self.pages(); pages[1]["settings"]["reasoningEffort"] = "low"
        with self.assertRaisesRegex(ao.RoomError, "identity changed"):
            self.observe(pages, False)

    def test_default_window_retains_5000_timeline_item_capacity(self):
        pages = []
        for i in range(50):
            page = self.pages()[0]
            page.update(turns=[], activities=[], messages=[{"id": str(j), "sequence": j} for j in range(5000-i*100-99, 5001-i*100)],
                        oldestSequence=5000-i*100-99, hasMoreBefore=i < 49)
            pages.append(page)
        raw, result = self.observe(pages)
        self.assertEqual(len(result["messages"]), 5000)
        self.assertEqual(raw.call_count, 50)

    def test_page_and_byte_bounds_never_certify_incomplete_history(self):
        for bound, value in (("MAX_PAGES", 1), ("MAX_REQUESTS", 1),
                             ("MAX_OBSERVATION_BYTES", len(history.canonical(self.pages()[0])) + 1)):
            with patch.object(history, bound, value):
                with self.subTest(bound=bound), self.assertRaisesRegex(ao.RoomError, "bounded observation window"):
                    self.observe(self.pages())
                raw, result = self.observe(self.pages(), False)
                self.assertTrue(result["history_truncated"])
                self.assertTrue(result["hasMoreBefore"])
                self.assertEqual([m["id"] for m in result["messages"]], ["m2"])

    def test_request_bound_includes_oversized_retries_without_advancing_cursor(self):
        for strict in (False, True):
            calls = []

            def fetch(method, path):
                calls.append((method, path))
                if len(calls) == 1:
                    return copy.deepcopy(self.pages()[0])
                raise history.ResponseTooLarge("synthetic oversized history GET")

            with self.subTest(strict=strict), patch.object(history, "MAX_REQUESTS", 3):
                if strict:
                    with self.assertRaisesRegex(ao.RoomError, "bounded observation window"):
                        history.conversation(fetch, "/sessions/fixture/conversation", strict=True)
                else:
                    result = history.conversation(fetch, "/sessions/fixture/conversation")
                    self.assertTrue(result["history_truncated"])
                    self.assertTrue(result["hasMoreBefore"])
                    self.assertEqual([m["id"] for m in result["messages"]], ["m2"])
            self.assertEqual(calls, [("GET", "/sessions/fixture/conversation?limit=100"),
                                    ("GET", "/sessions/fixture/conversation?limit=100&beforeSequence=20"),
                                    ("GET", "/sessions/fixture/conversation?limit=50&beforeSequence=20")])

    def test_elapsed_bound_fails_before_another_read(self):
        with patch.object(history.time, "monotonic", side_effect=[0, 0, 1, 151]):
            with self.assertRaisesRegex(ao.RoomError, "bounded observation window"):
                self.observe(self.pages())

    def test_invalid_json_evidence_and_sequences_refuse(self):
        for value in (float("nan"), "bad", True, -1):
            pages = self.pages(); pages[1]["messages"][0]["sequence"] = value
            with self.subTest(value=value), self.assertRaises(ao.RoomError):
                self.observe(pages)

    def test_complete_wrapper_remains_read_only_and_checks_lifecycle(self):
        client = ao.Client("http://127.0.0.1:1")
        reader = _CompleteClient(client)
        with patch.object(client, "request") as raw:
            for method, payload in (("POST", {}), ("DELETE", None), ("GET", {})):
                with self.assertRaises(ao.RoomError):
                    reader.request(method, "/sessions/fixture", payload)
            raw.assert_not_called()
        with patch.object(client, "request", return_value={"session": {"isTerminated": True}}):
            with self.assertRaisesRegex(ao.RoomError, "isTerminated=false"):
                reader.request("GET", "/sessions/fixture")


class HistoryWorkflowTests(Fixture):
    def setUp(self):
        super().setUp()
        original_request = self.fake.request
        original_conversation = self.fake.conversation
        self.history_paths = []
        for session in self.fake.sessions.values():
            session["isTerminated"] = False
        for page in self.fake.snapshots.values():
            page.update(controller="ready", branchMaterialization={"strategy": "native"},
                        activities=[], hasMoreBefore=False)
        def raw(method, path, payload=None):
            if method == "GET" and "/conversation?" in path:
                self.history_paths.append(path)
                self.assertIn("?limit=100", path)
                return original_conversation(path.split("/")[2])
            return original_request(method, path, payload)
        self.fake.request = raw
        self.fake.conversation = lambda name: ao.Client.conversation(self.fake, name)
        self.room = self.open()
        self.spec()

    def test_send_sync_verify_accept_flow_with_native_pagination_and_idempotency(self):
        self.bind(); self.agree(); self.implement(); self.review()
        self.assertTrue(self.service.ao_room_accept(self.room, "acceptance_review")["accepted"])
        self.assertEqual(len(self.fake.posts), 3)
        self.assertTrue(self.history_paths)
        self.assertEqual(self.send("implementation")["state"], "completed")
        self.assertEqual(len(self.fake.posts), 3)

    def test_incomplete_history_refuses_before_intent_or_dispatch(self):
        self.bind()
        self.fake.snapshots["engineer"]["hasMoreBefore"] = True
        with self.assertRaisesRegex(ao.RoomError, "pagination is incomplete"):
            self.send("spec_review")
        self.assertEqual(self.fake.posts, [])
        self.assertEqual(self.state()["requests"], {})

    def test_failed_turn_stays_uncertain_without_replay(self):
        self.bind(); self.send("spec_review")
        self.fake.finish("engineer", "Synthetic failure", state="failed")
        self.service.ao_room_sync(self.room)
        self.assertEqual(self.state()["requests"]["spec_review"]["state"], "uncertain")
        with self.assertRaisesRegex(ao.RoomError, "active or uncertain"):
            self.send("spec_review", "another")
        self.assertEqual(len(self.fake.posts), 1)

    def test_complete_identity_wrapper_uses_same_shared_reader(self):
        self.bind()
        state = self.state()
        snapshot = self.service.identity(_CompleteClient(self.service.client(state)), state, state["bindings"]["engineer"])
        self.assertFalse(snapshot["history_truncated"])
        self.assertEqual(snapshot["activities"], [])
        self.assertTrue(self.history_paths)
        self.assertEqual(self.fake.posts, [])


if __name__ == "__main__":
    unittest.main()
