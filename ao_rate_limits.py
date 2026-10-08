"""Read-only fold of the provider's own rate-limit readings for one AO session.

AO's bridge stores each Claude SDK ``rate_limit_event`` in the SQLite table
``conversation_provider_events`` (``method='account.rateLimits'``), one row per
API response, keyed by the AO ``session_id``. This view reads the newest rows
for the bound session through a read-only connection to the database path the
explicit outcome audit already recorded — no writes, no model, no network, and
unavailability is reported, never guessed.

Every percent is the provider's own figure: ``-1`` means the provider did not
report a reading at all — it is never a zero. The burn arithmetic summarizes
the reported readings only; ``minutes_remaining_at_current_rate`` is arithmetic
over those readings, not a forecast of the provider's policy.
"""

import datetime
import json
import os
from pathlib import Path
import sqlite3
import stat

SOURCE = "ao_provider_events"
QUERY = ("SELECT received_at, payload_json FROM conversation_provider_events "
         "WHERE session_id = ? AND method = 'account.rateLimits' "
         "ORDER BY received_at DESC LIMIT ?")
_STAMP_FORMATS = ("%Y-%m-%d %H:%M:%S.%f %z", "%Y-%m-%d %H:%M:%S %z")


def _unavailable(reason, **extra):
    return {"source": SOURCE, "available": False, "unavailable_reason": reason, **extra}


def _received_at(text):
    """One AO provider-event timestamp like '2026-10-07 23:49:24.123456 +0000 UTC'."""
    if not isinstance(text, str):
        return None
    candidate = text[:-4].rstrip() if text.endswith(" UTC") else text
    for fmt in _STAMP_FORMATS:
        try:
            return datetime.datetime.strptime(candidate, fmt)
        except ValueError:
            continue
    try:
        parsed = datetime.datetime.strptime(candidate, "%Y-%m-%d %H:%M:%S.%f")
    except ValueError:
        return None
    return parsed.replace(tzinfo=datetime.timezone.utc)


def _reading(row):
    """One row's timestamp and rateLimits object, or None when the row is malformed."""
    moment = _received_at(row[0])
    try:
        limits = json.loads(row[1]).get("rateLimits")
    except (TypeError, json.JSONDecodeError):
        return None
    if moment is None or not isinstance(limits, dict):
        return None
    fields = {}
    for name in ("PrimaryUsedPercent", "SecondaryUsedPercent", "PrimaryResetsInSeconds",
                 "SecondaryResetsInSeconds"):
        value = limits.get(name)
        if not isinstance(value, int) or isinstance(value, bool):
            return None
        fields[name] = value
    label = limits.get("PlanLabel")
    if label is not None and not isinstance(label, str):
        return None
    return {"moment": moment, "label": label, "primary": fields["PrimaryUsedPercent"],
            "primary_seconds": fields["PrimaryResetsInSeconds"],
            "secondary": fields["SecondaryUsedPercent"],
            "secondary_seconds": fields["SecondaryResetsInSeconds"]}


def _resets_at(moment, seconds):
    if not isinstance(seconds, int) or seconds < 0:
        return None
    return (moment + datetime.timedelta(seconds=seconds)).astimezone(datetime.timezone.utc).isoformat()


def _burn(newest, readings, window_seconds):
    basis = "reported readings with the newest row's label within the last " + str(window_seconds) + " seconds"
    samples = [row for row in readings
               if 0 <= newest["moment"].timestamp() - row["moment"].timestamp() <= window_seconds
               and row["label"] == newest["label"] and row["primary"] >= 0]
    slope, remaining = None, None
    if len(samples) >= 2:
        points = [(row["moment"].timestamp() / 60.0, row["primary"]) for row in samples]
        if max(t for t, _ in points) - min(t for t, _ in points) >= 1.0:
            mean_t = sum(t for t, _ in points) / len(points)
            mean_p = sum(p for _, p in points) / len(points)
            denominator = sum((t - mean_t) ** 2 for t, _ in points)
            if denominator > 0:
                slope = sum((t - mean_t) * (p - mean_p) for t, p in points) / denominator
                if slope > 0 and newest["primary"] >= 0:
                    remaining = (100 - newest["primary"]) / slope
    return {"basis": basis, "samples": len(samples),
            "percent_per_minute": None if slope is None else round(slope, 2),
            "minutes_remaining_at_current_rate": None if remaining is None else round(remaining, 2)}


def latest(database_path, session_id, *, now=None, window_seconds=600, max_rows=400):
    """The newest provider rate-limit reading for one AO session; never raises."""
    if not isinstance(database_path, str) or not database_path:
        return _unavailable("database_unbound")
    path = Path(database_path)
    try:
        safe = path.is_absolute() and not any(p.is_symlink() for p in (path, *path.parents))
        info = path.stat() if safe else None
    except OSError:
        info = None
    if info is None or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        return _unavailable("database_unreadable")
    try:
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5) as connection:
            rows = connection.execute(QUERY, (session_id, max_rows)).fetchall()
    except (sqlite3.Error, OSError):
        return _unavailable("database_unreadable")
    if not rows:
        return _unavailable("no_readings", rows_read=0, rows_malformed=0)
    readings, malformed = [], 0
    for row in rows:
        reading = _reading(row)
        if reading is None:
            malformed += 1
        else:
            readings.append(reading)
    if not readings:
        return _unavailable("no_readings", rows_read=len(rows), rows_malformed=malformed)
    newest = readings[0]
    moment = newest["moment"]
    if now is None:
        current = datetime.datetime.now(datetime.timezone.utc)
    elif isinstance(now, datetime.datetime):
        current = now
    else:
        current = datetime.datetime.fromtimestamp(now, datetime.timezone.utc)
    reported = newest["primary"] >= 0
    secondary_reported = newest["secondary"] >= 0
    return {"source": SOURCE, "available": True,
            "observed_at": moment.astimezone(datetime.timezone.utc).isoformat(),
            "age_seconds": (current - moment).total_seconds(),
            "window": {"label": newest["label"], "reported": reported,
                       "used_percent": newest["primary"] if reported else None,
                       "resets_in_seconds": newest["primary_seconds"] if reported else None,
                       "resets_at": _resets_at(moment, newest["primary_seconds"]) if reported else None},
            "secondary": {"reported": secondary_reported,
                          "used_percent": newest["secondary"] if secondary_reported else None,
                          "resets_in_seconds": newest["secondary_seconds"] if secondary_reported else None,
                          "resets_at": (_resets_at(moment, newest["secondary_seconds"])
                                        if secondary_reported else None)},
            "burn": _burn(newest, readings, window_seconds),
            "rows_read": len(rows), "rows_malformed": malformed}
