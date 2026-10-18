"""The ao_room_sync result's advisory rate_limits reading: the compact-status shape, read once per
call from the registered engineer's AO database; fake backends and a synthetic SQLite provider-event
database only; no account, network, AO process or model call."""
import datetime
from pathlib import Path
from unittest.mock import patch

import ao_rate_limits
from test_ao_lifecycle_contract import AgreedRoomFixture, PreparedRoomFixture
from test_ao_rate_limits import create_database, rate_limits_payload, write_event
from test_ao_sync_wait import FakeClock

AVAILABLE = {'label', 'used_percent', 'resets_at', 'minutes_remaining_at_current_rate', 'age_seconds'}


class SyncRateLimitsTests(AgreedRoomFixture):
    """The sync result carries the same provider reading the compact status view reports."""

    def _events(self, *rows):
        """Create the registered owner database with provider-event rows for the bound session."""
        source = self.state()['native_outcome_source']
        database = Path(source['database'])
        create_database(database)
        database.chmod(0o600)
        now = datetime.datetime.now(datetime.timezone.utc)
        for percent, seconds_ago, resets in rows:
            write_event(database, source['session_id'], 'account.rateLimits',
                        now - datetime.timedelta(seconds=seconds_ago),
                        rate_limits_payload(percent, resets=resets))

    def test_sync_result_carries_the_same_reading_as_compact_status(self):
        # One reset cycle: 90% five minutes ago, 95% now; the provider's seconds share the reset
        # instant, so the burn slope is 1.0 percent/minute and five minutes remain at that rate.
        self._events((90, 300, 2100), (95, 0, 1800))
        result = self.service.ao_room_sync(self.room)
        compact = self.service.ao_room_status(self.room, view='compact')
        self.assertEqual(set(result['rate_limits']), AVAILABLE)
        self.assertEqual(result['rate_limits']['label'], 'five hour')
        self.assertEqual(result['rate_limits']['used_percent'], 95)
        self.assertAlmostEqual(result['rate_limits']['minutes_remaining_at_current_rate'], 5.0, places=1)
        self.assertIsInstance(result['rate_limits']['age_seconds'], float)
        for key in AVAILABLE - {'age_seconds'}:
            self.assertEqual(result['rate_limits'][key], compact['rate_limits'][key])
        # age_seconds is live (now minus the provider event time): the two calls read at
        # different wall times, so pin it loosely; the other four keys pin the same reading.
        self.assertAlmostEqual(result['rate_limits']['age_seconds'],
                               compact['rate_limits']['age_seconds'], delta=60)
        through_mcp = self.mcp('ao_room_sync')
        self.assertEqual(set(through_mcp['rate_limits']), AVAILABLE)
        self.assertEqual(through_mcp['rate_limits']['used_percent'], 95)

    def test_reading_is_computed_once_per_sync_call_not_per_iteration(self):
        self._events((90, 300, 2100), (95, 0, 1800))
        self.send('implementation')  # stays running: the fake turn is never finished
        clock = FakeClock()
        with patch.object(ao_rate_limits, 'latest', wraps=ao_rate_limits.latest) as latest, \
                patch('ao_project_room.time.sleep', side_effect=clock.sleep), \
                patch('ao_project_room.time.monotonic', side_effect=clock.monotonic):
            result = self.service.ao_room_sync(self.room, wait_seconds=45)
        # FakeClock advances inside a reconciling iteration too (a flock retry or delegate poll's
        # real sleep routes through the patched stdlib time), so the stop reason is whichever the
        # measured loop reaches first — all three are the documented non-settlement reasons.
        self.assertIn(result['wait']['reason'], ('timeout', 'budget', 'interrupted'))
        self.assertGreaterEqual(result['wait']['iterations'], 3)
        self.assertEqual(latest.call_count, 1)
        # The wait object keeps its exact existing shape.
        self.assertEqual(set(result['wait']),
                         {'requested_seconds', 'reason', 'settled', 'iterations', 'elapsed_seconds',
                          'iteration_seconds', 'lock_wait_seconds'})
        self.assertEqual(result['rate_limits']['used_percent'], 95)

    def test_reading_never_changes_dispatch_or_state(self):
        self._events((90, 300, 2100), (95, 0, 1800))
        before = self.effects()
        result = self.service.ao_room_sync(self.room)
        self.assertEqual(result['rate_limits']['used_percent'], 95)
        # The only durable write a settled-room sync makes is its own routing observation
        # timestamp (REFUSAL_EFFECTS): the rate-limit read must add nothing beyond it.
        diff = self._diff_effects(before, self.effects())
        self.assertEqual(set(diff), {'state'})
        self.assertEqual(set(diff['state']), {'routing_rules.observed_at'})


class UnboundSourceTests(PreparedRoomFixture):
    """A room with no registered native outcome source reports the same reason compact status does."""

    def test_sync_result_is_fail_soft_without_a_database(self):
        result = self.service.ao_room_sync(self.room)
        compact = self.service.ao_room_status(self.room, view='compact')
        self.assertEqual(result['rate_limits'], {'unavailable_reason': 'database_unbound'})
        self.assertEqual(result['rate_limits'], compact['rate_limits'])
        # Every other sync field is exactly the full-status summary.
        status = self.service.ao_room_status(self.room)
        self.assertEqual({key: value for key, value in result.items() if key != 'rate_limits'},
                         status)
