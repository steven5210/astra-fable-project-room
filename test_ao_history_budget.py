"""Bounded large-history regression and exhaustion diagnostics; no sockets/models."""
import copy
import unittest
import urllib.parse
from unittest.mock import patch

import ao_history as history
from room import RoomError


class HistoryBudgetTests(unittest.TestCase):
    def pages(self):
        identity = {"sessionId": "fixture", "conversationId": "native", "activeBranchId": "native:root",
                    "controller": "ready", "settings": {"model": "fixture", "reasoningEffort": "max"},
                    "branchMaterialization": {"strategy": "native"}}
        return [{**copy.deepcopy(identity), "turns": [], "messages": [{"id": "new", "sequence": 2}],
                 "activities": [], "oldestSequence": 2, "hasMoreBefore": True},
                {**copy.deepcopy(identity), "turns": [], "messages": [{"id": "old", "sequence": 1}],
                 "activities": [], "oldestSequence": 1, "hasMoreBefore": False}]

    def large_timeline(self, *, conflict=False):
        # Each ~0.91MB page is below the unchanged8MB response cap. The complete
        # 12,500-entry timeline is above80MB and below160MB; all bounds stay live.
        body = "synthetic evidence " + "x" * 9000
        entries = [{"id": str(i), "sequence": i, "body": body} for i in range(1, 12501)]
        entries[0].update(status="failed", error="preserve oldest observed failure")
        entries[1].update(role="user", text="preserve original caller")
        base = self.pages()[0]
        calls = []
        def request(method, path):
            self.assertEqual(method, "GET")
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
            before = int(query.get("beforeSequence", [12501])[0])
            limit = int(query["limit"][0])
            selected = entries[max(0, before - 1 - limit):before - 1]
            page = {**base, "turns": [{"id": "original", "state": "failed" if conflict and selected[0]["sequence"] == 1 else "completed"}],
                    "messages": [x for x in selected if x["sequence"] % 2 == 0],
                    "activities": [x for x in selected if x["sequence"] % 2],
                    "oldestSequence": selected[0]["sequence"], "hasMoreBefore": selected[0]["sequence"] > 1}
            size = len(history.canonical(page))
            self.assertLess(size, history.MAX_RESPONSE_BYTES)
            calls.append(size)
            return page
        return entries, request, calls

    def test_real_text_timeline_preserves_complete_history(self):
        entries, request, calls = self.large_timeline()
        result = history.conversation(request, "/sessions/fixture/conversation", strict=True)
        self.assertGreater(sum(calls), 80_000_000)
        self.assertLess(sum(calls), 160_000_000)
        self.assertEqual(len(calls), 125)
        self.assertFalse(result["history_truncated"])
        self.assertFalse(result["hasMoreBefore"])
        self.assertEqual(sorted(result["messages"] + result["activities"], key=lambda x: x["sequence"]), entries)

    def test_oldest_conflict_beyond_previous_capacity_still_refuses(self):
        _, request, calls = self.large_timeline(conflict=True)
        with self.assertRaises(RoomError) as caught:
            history.conversation(request, "/sessions/fixture/conversation", strict=True)
        self.assertNotIsInstance(caught.exception, history.HistoryObservationLimit)
        self.assertGreater(sum(calls), 80_000_000)
        self.assertEqual(len(calls), 125)

    def test_aggregate_exact_edge_and_one_byte_excess(self):
        pages = self.pages()
        total = sum(len(history.canonical(p)) for p in pages)
        for allowance in (total, total + 1):
            with self.subTest(allowance=allowance), patch.object(history, "MAX_OBSERVATION_BYTES", allowance):
                result = history.conversation(lambda *args: pages.pop(0), "/sessions/fixture/conversation", strict=True)
                self.assertFalse(result["history_truncated"])
                self.assertEqual([x["id"] for x in result["messages"]], ["old", "new"])
            pages = self.pages()
        with patch.object(history, "MAX_OBSERVATION_BYTES", total - 1):
            with self.assertRaises(history.HistoryObservationLimit) as caught:
                history.conversation(lambda *args: pages.pop(0), "/sessions/fixture/conversation", strict=True)
            error = caught.exception
            self.assertEqual((error.limit_kind, error.limit, error.pages, error.requests, error.observed_bytes),
                             ("aggregate_bytes", total - 1, 2, 2, total))
            pages = self.pages()
            result = history.conversation(lambda *args: pages.pop(0), "/sessions/fixture/conversation")
            self.assertTrue(result["history_truncated"])
            self.assertTrue(result["hasMoreBefore"])
            self.assertEqual([x["id"] for x in result["messages"]], ["new"])

    def test_page_and_request_diagnostics_preserve_refusal(self):
        for bound, kind in (("MAX_PAGES", "pages"), ("MAX_REQUESTS", "requests")):
            pages = self.pages()
            with self.subTest(kind=kind), patch.object(history, bound, 1):
                with self.assertRaises(history.HistoryObservationLimit) as caught:
                    history.conversation(lambda *args: pages.pop(0), "/sessions/fixture/conversation", strict=True)
                self.assertEqual((caught.exception.limit_kind, caught.exception.limit, caught.exception.pages,
                                  caught.exception.requests), (kind, 1, 1, 1))
                self.assertEqual(len(pages), 1)

    def test_time_diagnostic_stops_before_another_get(self):
        pages = self.pages()
        with patch.object(history.time, "monotonic", side_effect=[0, 0, 1, 151]):
            with self.assertRaises(history.HistoryObservationLimit) as caught:
                history.conversation(lambda *args: pages.pop(0), "/sessions/fixture/conversation", strict=True)
        self.assertEqual((caught.exception.limit_kind, caught.exception.limit, caught.exception.pages,
                          caught.exception.requests), ("elapsed_seconds", 150, 1, 1))
        self.assertEqual(len(pages), 1)

    def test_oversized_response_accounting_reaches_byte_limit_without_retry(self):
        calls = []
        def request(method, path):
            calls.append((method, path))
            raise history.ResponseTooLarge()
        allowance = history.MAX_RESPONSE_BYTES + 1
        with patch.object(history, "MAX_OBSERVATION_BYTES", allowance):
            with self.assertRaises(history.HistoryObservationLimit) as caught:
                history.conversation(request, "/sessions/fixture/conversation", strict=True)
        self.assertEqual((caught.exception.limit_kind, caught.exception.limit, caught.exception.pages,
                          caught.exception.requests, caught.exception.observed_bytes),
                         ("aggregate_bytes", allowance, 0, 1, allowance))
        self.assertEqual(calls, [("GET", "/sessions/fixture/conversation?limit=100")])


if __name__ == "__main__":
    unittest.main()
