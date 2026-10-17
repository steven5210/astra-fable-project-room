"""Read-only delivery timing view of one AO room (ao_room_timing).

Folds the room's saved records — state.json requests, verification entries, acceptances and
saved receipt observed_at bounds — into labelled wall-clock intervals and the gaps between
them, so an operator can see where elapsed delivery time went without reading a transcript.
Bounded local reads only: no AO/network/model call, no room lock, no state write, no
transcript or rate-limit read, and malformed saved data degrades to limitation codes rather
than raising.
"""
import datetime
import json
import math
from pathlib import Path

from ao_delegate_launcher import owned_bytes
import ao_native_outcome
import ao_progress
import ao_prompt_metrics
from room import RoomError

VERSION = 1
MAX_INTERVALS = 200
MAX_GAPS = 200
MAX_ATTEMPTS = 50
MAX_PURPOSE_KEYS = 32
MAX_RECEIPT_BYTES = 64 * 1024 * 1024
MAX_FIELD_CHARS = 64

_LIVE_REQUEST_STATES = ("submitted", "running")
# Request states in which the turn has ended; a held terminal engineer turn opens a quota or
# hold wait gap. Kept equal to ao_project_room.TERMINAL; ao_project_room is imported lazily by
# its callers, so the set is pinned here and a test pins the equality.
_TERMINAL_REQUEST_STATES = {"completed", "failed", "cancelled", "interrupted", "settled_failure"}

CATEGORIES = ("engineering", "spec_review", "acceptance_review", "verification", "other")

MEANING = ("Wall-clock intervals and gaps folded from the room's saved request, hold and "
           "verification records; each category names the saved evidence behind it, not a "
           "cause; parallel worker activity inside a turn is not added and overlap is "
           "reported separately; nothing here is quota headroom, a stall verdict, a usage "
           "measurement or a speed-up claim.")

BASIS = {
    "engineering": "an engineer-role implementation or correction request, from its saved start to its saved end",
    "spec_review": "an engineer-role spec_review or spec_verdict_repair request",
    "acceptance_review": "a reviewer-role request",
    "verification": "a controller verification attempt with recorded start and finish times",
    "other": "a request whose role or purpose is outside the categories above",
    "quota_wait": "the gap after a terminal engineer turn whose saved semantic status kind is quota_limit, until the next recorded activity starts",
    "hold_wait": "the gap after a terminal engineer turn whose saved semantic status holds for any other kind; the saved records do not separate diagnosis, controller repair and operator latency inside it",
    "unattributed": "a gap that follows no held turn: operator or reviewer latency, decisions, integration and anything else the room does not record",
    "unknown_bounds": "a gap whose length cannot be measured because the preceding interval's end is unknown, inverted or still open; after an open interval every later gap stays unmeasured",
    "start": "the provider's startedAt from the observed turn when present, else the controller's created_at",
    "end": "the provider's completedAt from the observed turn when present, else the observed_at of the saved terminal receipt (an upper bound), else unknown; a live request is open",
    "overlap": "sum_seconds adds every interval, union_seconds merges them; overlap_seconds is the difference and is never added to wall time"}


def _iso(epoch):
    """One epoch second as a UTC ISO 8601 timestamp, the same style ao_rate_limits emits."""
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).isoformat()


def _seconds(value):
    """Seconds are floats rounded to three decimals."""
    return round(value, 3)


def _finite(value):
    """A finite, non-bool number; a huge saved int that overflows float conversion is not finite."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _epoch(value):
    """A saved numeric epoch that datetime can represent; anything else is unavailable."""
    if not _finite(value):
        return None
    try:
        datetime.datetime.fromtimestamp(float(value), datetime.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return float(value)


def _bounded(value):
    """The redacted, bounded display form of a saved id/purpose/state; absent or empty is None."""
    text, _clipped = ao_progress._bounded_text(value, MAX_FIELD_CHARS)
    return text


def _parse(value):
    """One saved ISO timestamp as an epoch second; a missing or unparseable value is None."""
    try:
        return ao_native_outcome.timestamp(value)
    except (RoomError, ValueError, OverflowError, OSError):
        return None


def _receipt_observed_at(directory, request, limitations=None):
    """The saved receipt's finite numeric observed_at — an upper bound on the turn's end — else None.

    Only a relative receipt path without '..' is read, through the bounded owned-bytes guard;
    every read or parse failure is None, never a raise.
    """
    receipt = request.get("receipt")
    if not isinstance(receipt, str):
        return None
    path = Path(receipt)
    if path.is_absolute() or ".." in path.parts:
        return None
    try:
        data = json.loads(owned_bytes(directory / receipt, MAX_RECEIPT_BYTES))
    except (OSError, ValueError):
        return None
    observed = data.get("observed_at") if isinstance(data, dict) else None
    if observed is not None and _epoch(observed) is None and limitations is not None:
        limitations.add("timestamp_unusable")
    return _epoch(observed)


def _category(role, purpose):
    """The measurement category a saved request belongs to, from its role and purpose only."""
    if role == "reviewer":
        return "acceptance_review"
    if role == "engineer" and purpose in ("implementation", "correction"):
        return "engineering"
    if role == "engineer" and purpose in ("spec_review", "spec_verdict_repair"):
        return "spec_review"
    return "other"


def _request_interval(directory, request_id, request, limitations):
    """One saved request as a timeline interval; missing evidence degrades, never raises."""
    semantic = request.get("semantic_status")
    if isinstance(semantic, dict):
        kind = semantic.get("kind") if isinstance(semantic.get("kind"), str) else None
        hold = semantic.get("hold") if isinstance(semantic.get("hold"), bool) else None
    elif isinstance(semantic, str):
        kind, hold = semantic, None
    else:
        kind, hold = None, None
    observed = request.get("observed_turn")
    start = _parse(observed.get("startedAt")) if isinstance(observed, dict) else None
    if start is not None:
        start_basis = "provider_started_at"
    else:
        created = _epoch(request.get("created_at"))
        if request.get("created_at") is not None and created is None:
            limitations.add("timestamp_unusable")
        start, start_basis = (created, "created_at") if created is not None else (None, "unknown")
        if start is None:
            limitations.add("start_time_unavailable")
    state = request.get("state")
    if isinstance(state, str) and state in _LIVE_REQUEST_STATES:
        end, end_basis = None, "open"
    else:
        end = _parse(observed.get("completedAt")) if isinstance(observed, dict) else None
        if end is not None:
            end_basis = "provider_completed_at"
        else:
            end = _receipt_observed_at(directory, request, limitations)
            end_basis = "receipt_observed_at" if end is not None else "unknown"
            if end is None:
                limitations.add("end_time_unavailable")
    if start is not None and end is not None and end < start:
        seconds = None
        limitations.add("clock_anomaly")
    else:
        seconds = end - start if start is not None and end is not None else None
    order = request.get("created_order")
    try:
        order_seconds = float(order) if _finite(order) else 0.0
    except OverflowError:
        order_seconds = 0.0
    if order is not None and not _finite(order):
        limitations.add("timestamp_unusable")
    interval = {"kind": "request", "request_id": _bounded(request_id), "role": _bounded(request.get("role")),
                "purpose": _bounded(request.get("purpose")), "state": _bounded(state),
                "category": _category(request.get("role"), request.get("purpose")),
                "started_at": _iso(start) if start is not None else None, "start_basis": start_basis,
                "ended_at": _iso(end) if end is not None else None, "end_basis": end_basis,
                "seconds": _seconds(seconds) if seconds is not None else None,
                "semantic_kind": _bounded(kind), "hold": hold,
                # Timeline bookkeeping; stripped before the interval is emitted.
                "_id": _bounded(request_id), "_start": start, "_end": end,
                "_order": order_seconds,
                "_raw_role": request.get("role"), "_raw_state": state,
                "_semantic_kind": kind, "_hold": hold}
    return interval


def _verification_entry(entry, limitations):
    """One saved verification attempt row plus, when its start is recorded, a timeline interval.

    Attempts saved before start/finish times existed keep their place in the ledger as
    not_recorded; they cannot enter the timeline without a start.
    """
    started, finished = _epoch(entry.get("started_at")), _epoch(entry.get("finished_at"))
    if entry.get("started_at") is not None and started is None:
        limitations.add("timestamp_unusable")
    if entry.get("finished_at") is not None and finished is None:
        limitations.add("timestamp_unusable")
    timing = "recorded" if started is not None else "not_recorded"
    if timing == "not_recorded":
        limitations.add("verification_timing_not_recorded")
    seconds = (finished - started
               if started is not None and finished is not None and finished >= started else None)
    attempt = {"id": _bounded(entry.get("id")), "state": _bounded(entry.get("state")),
               "started_at": _iso(started) if started is not None else None,
               "finished_at": _iso(finished) if finished is not None else None,
               "seconds": _seconds(seconds) if seconds is not None else None,
               "timing": timing}
    interval = None
    if started is not None:
        if finished is not None:
            end, end_basis = finished, "recorded"
        elif entry.get("state") == "running":
            end, end_basis = None, "open"
        else:
            end, end_basis = None, "unknown"
            limitations.add("end_time_unavailable")
        if finished is not None and finished < started:
            limitations.add("clock_anomaly")
        interval = {"kind": "verification", "id": _bounded(entry.get("id")), "role": "controller",
                    "purpose": "verification", "state": _bounded(entry.get("state")),
                    "category": "verification",
                    "started_at": _iso(started), "start_basis": "recorded",
                    "ended_at": _iso(end) if end is not None else None, "end_basis": end_basis,
                    "seconds": _seconds(seconds) if seconds is not None else None,
                    "_id": _bounded(entry.get("id")), "_start": started, "_end": end,
                    "_order": 0.0, "_raw_role": "controller", "_raw_state": entry.get("state"),
                    "_semantic_kind": None, "_hold": None}
    return attempt, interval


def _gaps(timeline):
    """Chronological gaps between the sorted intervals, classified only from saved evidence.

    The frontier is the furthest known valid end (end >= start) and its interval; an interval
    whose end is open, unknown or inverted is an unresolved predecessor, never a frontier.
    Two markers: a transient one for the last terminal interval whose end is unknown or
    inverted — bounded by the next later activity (a new send requires a settled room), so
    only that one gap is unmeasured — and a sticky one for the first open interval, which
    stays unresolved across every later interval because activity never proves it ended.
    """
    gaps = []
    frontier, frontier_source = None, None
    transient = None
    open_source = None
    for interval in timeline:
        start, end = interval["_start"], interval["_end"]
        if transient is not None:
            # A terminal predecessor's unknown or inverted end is bounded by this later
            # activity (a new send requires a settled room), so only this one gap is
            # unmeasured; the sticky open predecessor still names its own gaps afterwards.
            gaps.append({"kind": "unknown_bounds", "after": transient["source"]["_id"],
                         "before": interval["_id"], "started_at": None,
                         "ended_at": _iso(start), "seconds": None,
                         "semantic_kind": transient["source"].get("semantic_kind"),
                         "after_end_basis": transient["basis"]})
            transient = None
        elif open_source is not None:
            # The first open interval stays unresolved: later activity never proves it
            # ended, so every later gap stays unmeasured, named after it.
            gaps.append({"kind": "unknown_bounds", "after": open_source["_id"],
                         "before": interval["_id"], "started_at": None,
                         "ended_at": _iso(start), "seconds": None,
                         "semantic_kind": open_source.get("semantic_kind"),
                         "after_end_basis": "open"})
        elif frontier is not None and start > frontier:
            kind = "unattributed"
            if (frontier_source["kind"] == "request" and frontier_source["_raw_role"] == "engineer"
                    and isinstance(frontier_source["_raw_state"], str)
                    and frontier_source["_raw_state"] in _TERMINAL_REQUEST_STATES
                    and frontier_source["_hold"] is True):
                kind = "quota_wait" if frontier_source["_semantic_kind"] == "quota_limit" else "hold_wait"
            gaps.append({"kind": kind, "after": frontier_source["_id"], "before": interval["_id"],
                         "started_at": _iso(frontier), "ended_at": _iso(start),
                         "seconds": _seconds(start - frontier),
                         "semantic_kind": frontier_source.get("semantic_kind"),
                         "after_end_basis": None})
        if interval["end_basis"] == "open":
            if open_source is None:
                open_source = interval
        elif end is None:
            transient = {"source": interval, "basis": "unknown"}
        elif end < start:
            transient = {"source": interval, "basis": "anomaly"}
        elif frontier is None or end > frontier:
            frontier, frontier_source = end, interval
    return gaps


def _add_seconds(histogram, key, seconds):
    """A _bump-style bounded fold that sums seconds: named keys stop before 'other' folds the rest."""
    if key not in histogram and len(histogram) - ("other" in histogram) >= MAX_PURPOSE_KEYS - 1:
        key = "other"
    histogram[key] = histogram.get(key, 0.0) + seconds


def inspect(directory, state):
    """The room's read-only delivery timing view; malformed saved data degrades to limitation codes."""
    limitations = {"native_worker_usage_not_attributed"}
    directory = Path(directory)
    state = state if isinstance(state, dict) else {}
    requests = state.get("requests")
    requests = requests if isinstance(requests, dict) else {}
    verifications = state.get("verifications")
    verifications = verifications if isinstance(verifications, list) else []
    acceptances = state.get("acceptances")
    acceptances = acceptances if isinstance(acceptances, list) else []

    intervals = []
    for request_id, request in requests.items():
        if not isinstance(request, dict):
            continue
        intervals.append(_request_interval(directory, request_id, request, limitations))
    attempts = []
    for entry in verifications:
        if not isinstance(entry, dict):
            continue
        attempt, interval = _verification_entry(entry, limitations)
        attempts.append(attempt)
        if interval is not None:
            intervals.append(interval)

    # The timeline holds every interval whose start is known, sorted by start then the saved
    # request order; a request with no known start is still emitted, after the timeline.
    timeline = sorted((iv for iv in intervals if iv["_start"] is not None),
                      key=lambda iv: (iv["_start"], iv["_order"]))
    unknown_start = sorted((iv for iv in intervals if iv["_start"] is None),
                           key=lambda iv: iv["_order"])
    ordered = timeline + unknown_start

    gaps_all = _gaps(timeline)
    gap_totals = {"quota_wait": 0.0, "hold_wait": 0.0, "unattributed": 0.0, "unknown_bounds": 0}
    for gap in gaps_all:
        if gap["kind"] == "unknown_bounds":
            gap_totals["unknown_bounds"] += 1
        else:
            gap_totals[gap["kind"]] += gap["seconds"]
    for kind in ("quota_wait", "hold_wait", "unattributed"):
        gap_totals[kind] = _seconds(gap_totals[kind])

    spans = sorted((iv["_start"], iv["_end"]) for iv in timeline
                   if iv["_end"] is not None and iv["_end"] >= iv["_start"])
    union_seconds, current_start, current_end = 0.0, None, None
    for start, end in spans:
        if current_start is None:
            current_start, current_end = start, end
        elif start <= current_end:
            current_end = max(current_end, end)
        else:
            union_seconds += current_end - current_start
            current_start, current_end = start, end
    if current_start is not None:
        union_seconds += current_end - current_start
    sum_seconds = sum(end - start for start, end in spans)

    by_category = {name: 0.0 for name in CATEGORIES}
    by_purpose = {}
    for interval in intervals:
        if interval["seconds"] is None:
            continue
        by_category[interval["category"]] += interval["seconds"]
        key = "verification" if interval["kind"] == "verification" else (interval["purpose"] or "unknown")
        _add_seconds(by_purpose, key, interval["seconds"])

    starts = [iv["_start"] for iv in intervals if iv["_start"] is not None]
    ends = [iv["_end"] for iv in intervals if iv["_end"] is not None]
    first_started = min(starts) if starts else None
    last_known = max(starts + ends) if starts or ends else None
    start_basis_counts = {"provider_started_at": 0, "created_at": 0}
    end_basis_counts = {"provider_completed_at": 0, "receipt_observed_at": 0, "open": 0, "unknown": 0}
    provider_bases, controller_bases = {"provider_started_at", "provider_completed_at"}, \
        {"created_at", "receipt_observed_at", "recorded"}
    used_bases = set()
    for interval in intervals:
        # The two histograms carry their fixed keys only; a verification's "recorded" basis is
        # not a start/end key but still counts toward clock_sources_mixed below.
        if interval["start_basis"] in start_basis_counts:
            start_basis_counts[interval["start_basis"]] += 1
        if interval["end_basis"] in end_basis_counts:
            end_basis_counts[interval["end_basis"]] += 1
        used_bases.add(interval["start_basis"])
        used_bases.add(interval["end_basis"])
    if used_bases & provider_bases and used_bases & controller_bases:
        limitations.add("clock_sources_mixed")

    verdicts = {"passed": 0, "failed": 0, "running": 0, "other": 0}
    for entry in verifications:
        if isinstance(entry, dict):
            name = entry.get("state")
            verdicts[name if isinstance(name, str) and name in verdicts else "other"] += 1
    recorded_acceptances = [entry for entry in acceptances if isinstance(entry, dict)]

    intervals_truncated = len(ordered) > MAX_INTERVALS
    gaps_truncated = len(gaps_all) > MAX_GAPS
    if intervals_truncated:
        limitations.add("intervals_truncated")
    if gaps_truncated:
        limitations.add("gaps_truncated")
    shown_intervals = ordered[-MAX_INTERVALS:]
    shown_gaps = gaps_all[-MAX_GAPS:]
    for interval in intervals:
        for private in ("_id", "_start", "_end", "_order", "_raw_role", "_raw_state",
                        "_semantic_kind", "_hold"):
            interval.pop(private, None)

    return {"version": VERSION, "room_id": state.get("room_id"),
            "intervals": shown_intervals,
            "intervals_total": len(ordered), "intervals_truncated": intervals_truncated,
            "gaps": shown_gaps,
            "gaps_total": len(gaps_all), "gaps_truncated": gaps_truncated,
            "verification": {"attempts": attempts[-MAX_ATTEMPTS:], "attempts_total": len(attempts),
                             "recorded": sum(a["timing"] == "recorded" for a in attempts),
                             "not_recorded": sum(a["timing"] == "not_recorded" for a in attempts)},
            "totals": {"by_category": {name: _seconds(value) for name, value in by_category.items()},
                       "by_purpose": {key: _seconds(value) for key, value in by_purpose.items()},
                       "gaps": gap_totals,
                       "sum_seconds": _seconds(sum_seconds), "union_seconds": _seconds(union_seconds),
                       "overlap_seconds": _seconds(max(0.0, sum_seconds - union_seconds))},
            "coverage": {"first_started_at": _iso(first_started) if first_started is not None else None,
                         "last_known_at": _iso(last_known) if last_known is not None else None,
                         "span_seconds": (_seconds(last_known - first_started)
                                          if first_started is not None and last_known is not None
                                          else None),
                         "open_intervals": end_basis_counts["open"],
                         "end_unknown": end_basis_counts["unknown"],
                         "start_unknown": len(unknown_start),
                         "start_basis": start_basis_counts,
                         "end_basis": end_basis_counts},
            "closure": {"verifications": verdicts,
                        "acceptances_recorded": len(recorded_acceptances),
                        "latest_acceptance_request_id": (_bounded(recorded_acceptances[-1].get("request_id"))
                                                       if recorded_acceptances else None),
                        "note": "saved verification and acceptance records only; task counts are not closure"},
            "native_worker_usage": dict(ao_prompt_metrics.NATIVE_WORKER_USAGE),
            "basis": dict(BASIS), "limitations": sorted(limitations), "meaning": MEANING}
