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
        self.event(-1, 30, resets=13429, secondary_resets=0)
        value = self.latest()
        self.assertTrue(value["available"])
        self.assertEqual(value["observed_at"], (self.now - datetime.timedelta(seconds=30)).isoformat())
        self.assertFalse(value["window"]["reported"])
        self.assertIsNone(value["window"]["used_percent"])
        # Reset times ride on the provider's own seconds, independently of the percent;
        # a reset of 0 means no window is running and reports null.
        self.assertEqual(value["window"]["resets_in_seconds"], 13429)
        self.assertEqual(value["window"]["resets_at"],
                         (self.now - datetime.timedelta(seconds=30)
                          + datetime.timedelta(seconds=13429)).isoformat())
        self.assertIsNone(value["secondary"]["resets_in_seconds"])
        self.assertIsNone(value["secondary"]["resets_at"])
        self.assertEqual(value["burn"]["samples"], 0)
        self.assertIsNone(value["burn"]["percent_per_minute"])
        self.assertIsNone(value["burn"]["minutes_remaining_at_current_rate"])
        self.assertEqual(value["rows_read"], 2)

    def test_a_reported_sequence_carries_slope_and_remaining_minutes(self):
        # One reset cycle: the provider's seconds count down, so all three rows
        # share the reset instant now + 1800 s.
        self.event(90, 300, resets=2100)
        self.event(92, 150, resets=1950)
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
        self.event(72, 0)
        value = self.latest()
        self.assertIsNone(value["burn"]["percent_per_minute"])
        self.event(70, -20, session="other")
        with sqlite3.connect(self.database) as connection:
            payload = {"rateLimits": {"PrimaryUsedPercent": 70, "SecondaryUsedPercent": -1,
                                      "PrimaryResetsInSeconds": 3620, "SecondaryResetsInSeconds": -1,
                                      "PlanLabel": "five hour", "CodexCapacity": None}}
            connection.execute("INSERT INTO conversation_provider_events VALUES (?,?,?,?)",
                               ("session-1", "account.rateLimits",
                                stamp(self.now - datetime.timedelta(seconds=20)), json.dumps(payload)))
        value = self.latest()
        self.assertEqual(value["burn"]["samples"], 2)
        self.assertIsNone(value["burn"]["percent_per_minute"])

    def test_burn_stays_inside_the_newest_reset_cycle(self):
        # The reviewer's live shape: 0%, then 95%, then a fresh-cycle 5% — all
        # "five hour". The older rows' reset instants belong to the old cycle.
        self.event(0, 540, resets=120)
        self.event(95, 120, resets=300)
        self.event(5, 0, resets=3600)
        value = self.latest()
        self.assertEqual(value["window"]["used_percent"], 5)
        self.assertEqual(value["burn"]["samples"], 1)
        self.assertIsNone(value["burn"]["percent_per_minute"])
        self.assertIsNone(value["burn"]["minutes_remaining_at_current_rate"])

    def test_burn_stops_at_a_percent_drop_when_reset_instants_match(self):
        # Same reset instant (the countdown keeps one instant), but 95% two
        # minutes ago cannot precede 5% now inside one cycle.
        self.event(90, 300, resets=3900)
        self.event(95, 120, resets=3720)
        self.event(5, 0, resets=3600)
        value = self.latest()
        self.assertEqual(value["burn"]["samples"], 1)
        self.assertIsNone(value["burn"]["percent_per_minute"])

    def test_burn_falls_back_to_the_monotone_rule_without_reset_seconds(self):
        # No reset seconds at all: the newest row is 7%, one minute ago; 3% kept
        # beside it, and 97% seven minutes ago belongs to the previous cycle.
        self.event(97, 420, resets=-1)
        self.event(3, 180, resets=-1)
        self.event(7, 60, resets=-1)
        value = self.latest()
        self.assertEqual(value["window"]["used_percent"], 7)
        self.assertEqual(value["burn"]["samples"], 2)
        self.assertEqual(value["burn"]["percent_per_minute"], 2.0)

    def test_out_of_range_reset_seconds_report_null_without_malforming(self):
        self.event(80, 0, resets=10 ** 15, secondary=40, secondary_resets=10 ** 20)
        value = self.latest()
        self.assertTrue(value["available"])
        self.assertEqual(value["window"]["used_percent"], 80)
        self.assertIsNone(value["window"]["resets_in_seconds"])
        self.assertIsNone(value["window"]["resets_at"])
        self.assertIsNone(value["secondary"]["resets_at"])
        self.assertEqual(value["rows_malformed"], 0)

    def test_malformed_rows_are_skipped_and_counted(self):
        self.event(91, 60)
        with sqlite3.connect(self.database) as connection:
            connection.execute("INSERT INTO conversation_provider_events VALUES (?,?,?,?)",
                               ("session-1", "account.rateLimits",
                                stamp(self.now - datetime.timedelta(seconds=30)), "not json"))
            connection.execute("INSERT INTO conversation_provider_events VALUES (?,?,?,?)",
                               ("session-1", "account.rateLimits",
                                stamp(self.now - datetime.timedelta(seconds=10)),
                                json.dumps({"rateLimits": {"PrimaryUsedPercent": "high"}})))
            connection.execute("INSERT INTO conversation_provider_events VALUES (?,?,?,?)",
                               ("session-1", "account.rateLimits",
                                stamp(self.now - datetime.timedelta(seconds=5)), "null"))
        value = self.latest()
        self.assertTrue(value["available"])
        self.assertEqual(value["window"]["used_percent"], 91)
        self.assertEqual(value["rows_read"], 4)
        self.assertEqual(value["rows_malformed"], 3)

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
