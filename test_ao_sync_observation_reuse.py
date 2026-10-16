"""Per-iteration AO observation reuse: offline fakes only, no account/model calls."""
import contextlib
import copy
import json
import re
import unittest
from unittest.mock import patch

import ao_history
import ao_project_room as ao
import ao_history_reconciliation as reconciliation
from ao_history_reconciliation import StrictHistoryClient
from ao_provider_transition import _CompleteClient
from test_ao_normal import Fixture


class ObservationReuseTests(Fixture):
    def setUp(self):
        super().setUp()
        self.room = self.open()
        self.spec()

    @contextlib.contextmanager
    def observed_counts(self):
        """Count conversation walks and exact session GETs during a measured operation.

        FakeAO.conversation bypasses request(), so both surfaces are wrapped: the
        walk counter rides fake.conversation directly and session GETs are matched
        by exact /sessions/<id> paths in fake.request.
        """
        original_request = self.fake.request
        session_gets = []

        def counting_request(method, path, payload=None):
            if method == "GET" and re.fullmatch(r"/sessions/[^/?]+", path):
                session_gets.append(path)
            return original_request(method, path, payload)

        with patch.object(self.fake, "conversation", wraps=self.fake.conversation) as walks, \
                patch.object(self.fake, "request", side_effect=counting_request):
            yield walks, session_gets

    def three_completed_and_one_running(self):
        """spec_review + implementation + correction completed; a second correction is running."""
        self.bind()
        self.agree()
        self.implement()
        self.send("correction")
        self.fake.finish("engineer", json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        self.send("correction", "correction-two")

    def test_one_iteration_reuses_one_walk_and_one_session_get_per_session(self):
        self.three_completed_and_one_running()
        with self.observed_counts() as (walks, session_gets), \
                patch.object(self.service, "identity", wraps=self.service.identity) as identity:
            self.service._sync_once(self.room)
        self.assertGreaterEqual(identity.call_count, 4)
        self.assertEqual(walks.call_count, 1)
        self.assertEqual(session_gets, ["/sessions/engineer"])

    def test_one_walk_per_bound_session(self):
        self.bind()
        self.agree()
        self.implement()
        self.send("correction")
        self.fake.finish("engineer", json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        self.review()
        with self.observed_counts() as (walks, session_gets):
            self.service._sync_once(self.room)
        self.assertEqual(sorted(call.args[0] for call in walks.call_args_list), ["engineer", "reviewer"])
        self.assertEqual(sorted(session_gets), ["/sessions/engineer", "/sessions/reviewer"])

    def test_reader_policies_key_the_observation_cache(self):
        self.assertEqual(ao.reader_policy(self.fake), ())
        self.assertEqual(ao.reader_policy(StrictHistoryClient(self.fake)), ("StrictHistoryClient",))
        self.assertEqual(ao.reader_policy(_CompleteClient(self.fake)), ("_CompleteClient",))
        self.assertEqual(ao.reader_policy(StrictHistoryClient(_CompleteClient(self.fake))),
                         ("StrictHistoryClient", "_CompleteClient"))
        # StrictHistoryClient walks through ao_history.conversation over request(); the fake
        # serves no paged history, so count the strict reader's calls with a canned snapshot.
        paged_calls = []

        def paged(request, path, **kwargs):
            paged_calls.append(path)
            return copy.deepcopy(self.fake.snapshots["engineer"])

        observations = ao.IterationObservations()
        with patch.object(self.fake, "conversation", wraps=self.fake.conversation) as direct, \
                patch.object(ao_history, "conversation", side_effect=paged):
            ordinary = observations.history(self.fake, "engineer")
            strict = observations.history(StrictHistoryClient(self.fake), "engineer")
            repeated = observations.history(self.fake, "engineer")
        self.assertIs(repeated, ordinary)
        self.assertIsNot(strict, ordinary)
        self.assertEqual(direct.call_count, 1)
        self.assertEqual(len(paged_calls), 1)
        self.assertEqual(len(observations.histories), 2)

    def test_each_iteration_observes_freshly(self):
        self.bind()
        self.send("spec_review")
        with self.observed_counts() as (walks, _session_gets):
            self.service._sync_once(self.room)
            spec = self.service.spec(self.directory(), self.state())
            self.fake.finish("engineer", json.dumps(
                {"interpretation": "Implement the supplied pure behavior.", "findings": [],
                 "decision": "accept", "spec_revision": 1, "spec_sha256": spec["sha256"]}))
            _summary, states = self.service._sync_once(self.room)
        self.assertEqual(walks.call_count, 2)
        self.assertEqual(states, {"spec_review": "completed"})

    def test_shared_snapshots_are_not_mutated_during_the_iteration(self):
        self.three_completed_and_one_running()
        recorded = []

        class Recording(ao.IterationObservations):
            def session(self, client, session_id):
                key = (ao.reader_policy(client), session_id)
                if key not in self.sessions:
                    value = super().session(client, session_id)
                    recorded.append((value, ao_history.canonical(value)))
                return self.sessions[key]

            def history(self, client, session_id):
                key = (ao.reader_policy(client), session_id)
                if key not in self.histories:
                    value = super().history(client, session_id)
                    recorded.append((value, ao_history.canonical(value)))
                return self.histories[key]

        with patch.object(ao, "IterationObservations", Recording):
            self.service._sync_once(self.room)
        self.assertTrue(recorded)
        for value, before in recorded:
            self.assertEqual(ao_history.canonical(value), before)

    def test_truncated_history_records_identical_with_or_without_reuse(self):
        self.bind()
        self.agree()
        self.implement()
        self.send("correction")
        self.fake.snapshots["engineer"]["history_truncated"] = True
        self.fake.finish("engineer", json.dumps(self.report()))
        # Keep the receipt capture on the ordinary reader so the comparison below
        # exercises only the reuse, not the separate strict-history lane.
        with patch.object(reconciliation, "requires_strict_history", return_value=False):
            self.service._sync_once(self.room)
            receipt = self.state()["requests"]["correction"]["receipt"]
            self.assertTrue(ao.read(self.directory() / receipt)["history_truncated"])
            cached = self.state()["requests"]

            class NoStore(ao.IterationObservations):
                def session(self, client, session_id):
                    return client.request("GET", "/sessions/" + session_id)

                def history(self, client, session_id):
                    return client.conversation(session_id)

            with patch.object(ao, "IterationObservations", NoStore):
                self.service._sync_once(self.room)
            uncached = self.state()["requests"]
        self.assertEqual(cached, uncached)

    def test_a_failed_walk_caches_nothing_and_propagates(self):
        self.bind()
        self.agree()
        created = []

        class Recording(ao.IterationObservations):
            def __init__(self):
                super().__init__()
                created.append(self)

        original = self.fake.conversation

        def failing(name):
            if name == "engineer":
                raise ao.RoomError("Synthetic history failure")
            return original(name)

        with patch.object(ao, "IterationObservations", Recording), \
                patch.object(self.fake, "conversation", side_effect=failing):
            with self.assertRaisesRegex(ao.RoomError, "Synthetic history failure"):
                self.service._sync_once(self.room)
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].histories, {})


if __name__ == "__main__":
    unittest.main()
