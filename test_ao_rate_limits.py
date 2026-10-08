"""The provider rate-limit view over synthetic AO provider-event databases.

Every database below is a real temporary SQLite file read through the module's
own read-only connection; no AO process, account or network is involved.
"""

import datetime
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import ao_rate_limits


def stamp(moment):
    """The AO bridge's received_at format: '2026-10-07 23:49:24.123456 +0000 UTC'."""
    return moment.strftime("%Y-%m-%d %H:%M:%S.%f +0000 UTC")


class RateLimitViewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Path(self.temporary.name).resolve() / "ao.db"
        self.now = datetime.datetime(2026, 10, 8, 0, 0, tzinfo=datetime.timezone.utc)
        with sqlite3.connect(self.database) as connection:
            connection.execute("CREATE TABLE conversation_provider_events "
                               "(session_id TEXT, method TEXT, received_at TEXT, payload_json TEXT)")

    def event(self, percent, seconds_ago, *, session="session-1", label="five hour", resets=3600,
              secondary=-1, secondary_resets=86400, method="account.rateLimits", payload=None):
        if payload is None:
            payload = {"rateLimits": {"PrimaryUsedPercent": percent,
                                      "SecondaryUsedPercent": secondary,
                                      "PrimaryResetsInSeconds": resets,
                                      "SecondaryResetsInSeconds": secondary_resets,
                                      "PlanLabel": label, "CodexCapacity": None}}
        moment = self.now - datetime.timedelta(seconds=seconds_ago)
        with sqlite3.connect(self.database) as connection:
            connection.execute("INSERT INTO conversation_provider_events VALUES (?,?,?,?)",
                               (session, method, stamp(moment), json.dumps(payload)))

    def latest(self, **kwargs):
        return ao_rate_limits.latest(str(self.database), "session-1", now=self.now, **kwargs)

    def test_unreported_rows_are_available_but_carry_no_figure(self):
        self.event(-1, 60)
        self.event(-1, 30)
        value = self.latest()
        self.assertTrue(value["available"])
        self.assertEqual(value["observed_at"], (self.now - datetime.timedelta(seconds=30)).isoformat())
        self.assertFalse(value["window"]["reported"])
        self.assertIsNone(value["window"]["used_percent"])
        self.assertIsNone(value["window"]["resets_at"])
        self.assertEqual(value["burn"]["samples"], 0)
        self.assertIsNone(value["burn"]["percent_per_minute"])
        self.assertIsNone(value["burn"]["minutes_remaining_at_current_rate"])
        self.assertEqual(value["rows_read"], 2)

    def test_a_reported_sequence_carries_slope_and_remaining_minutes(self):
        self.event(90, 300)
        self.event(92, 150)
        self.event(95, 0, secondary=63, resets=1800)
        value = self.latest()
        self.assertEqual(value["window"]["label"], "five hour")
        self.assertEqual(value["window"]["used_percent"], 95)
        self.assertEqual(value["window"]["resets_in_seconds"], 1800)
        self.assertEqual(value["window"]["resets_at"],
                         (self.now + datetime.timedelta(seconds=1800)).isoformat())
        self.assertTrue(value["secondary"]["reported"])
        self.assertEqual(value["secondary"]["used_percent"], 63)
        self.assertEqual(value["burn"]["samples"], 3)
        self.assertAlmostEqual(value["burn"]["percent_per_minute"], 1.0, places=2)
        self.assertAlmostEqual(value["burn"]["minutes_remaining_at_current_rate"], 5.0, places=1)
        self.assertEqual(value["age_seconds"], 0.0)

    def test_earlier_different_labels_are_excluded_from_the_burn(self):
        self.event(80, 200, label="five hour")
        self.event(90, 100, label="five hour")
        self.event(60, 0, label="seven day")
        value = self.latest()
        self.assertEqual(value["window"]["label"], "seven day")
        self.assertEqual(value["window"]["used_percent"], 60)
        self.assertEqual(value["burn"]["samples"], 1)
        self.assertIsNone(value["burn"]["percent_per_minute"])
        self.assertIsNone(value["burn"]["minutes_remaining_at_current_rate"])

    def test_insufficient_samples_and_short_spans_give_no_slope(self):
        self.event(70, 0)
        value = self.latest()
        self.assertIsNone(value["burn"]["percent_per_minute"])
        self.event(72, -20, session="other")
        with sqlite3.connect(self.database) as connection:
            payload = {"rateLimits": {"PrimaryUsedPercent": 72, "SecondaryUsedPercent": -1,
                                      "PrimaryResetsInSeconds": 60, "SecondaryResetsInSeconds": -1,
                                      "PlanLabel": "five hour", "CodexCapacity": None}}
            connection.execute("INSERT INTO conversation_provider_events VALUES (?,?,?,?)",
                               ("session-1", "account.rateLimits",
                                stamp(self.now - datetime.timedelta(seconds=20)), json.dumps(payload)))
        value = self.latest()
        self.assertEqual(value["burn"]["samples"], 2)
        self.assertIsNone(value["burn"]["percent_per_minute"])

    def test_malformed_rows_are_skipped_and_counted(self):
        self.event(90, 60)
        with sqlite3.connect(self.database) as connection:
            connection.execute("INSERT INTO conversation_provider_events VALUES (?,?,?,?)",
                               ("session-1", "account.rateLimits",
                                stamp(self.now - datetime.timedelta(seconds=30)), "not json"))
            connection.execute("INSERT INTO conversation_provider_events VALUES (?,?,?,?)",
                               ("session-1", "account.rateLimits",
                                stamp(self.now - datetime.timedelta(seconds=10)),
                                json.dumps({"rateLimits": {"PrimaryUsedPercent": "high"}})))
        value = self.latest()
        self.assertTrue(value["available"])
        self.assertEqual(value["window"]["used_percent"], 90)
        self.assertEqual(value["rows_read"], 3)
        self.assertEqual(value["rows_malformed"], 2)

    def test_unavailable_readings_never_raise_and_never_guess(self):
        self.assertEqual(self.latest_unbound()["unavailable_reason"], "database_unbound")
        missing = Path(self.temporary.name).resolve() / "missing.db"
        value = ao_rate_limits.latest(str(missing), "session-1", now=self.now)
        self.assertEqual(value["unavailable_reason"], "database_unreadable")
        value = self.latest()
        self.assertEqual(value["unavailable_reason"], "no_readings")
        self.event(50, 0, session="other-session")
        value = self.latest()
        self.assertEqual(value["unavailable_reason"], "no_readings")

    def latest_unbound(self):
        return ao_rate_limits.latest(None, "session-1", now=self.now)


if __name__ == '__main__':
    unittest.main()
