"""Bounded AO history observations shared by ordinary reads and strict audits.

Only an oversized, body-free history GET may be repeated with a smaller page.
Dispatch, lifecycle calls, network failures and ambiguous evidence are never retried.
"""

import json
import time

from room import RoomError


PAGE_ITEMS = 100
MAX_PAGES = 200  # Up to 20,000 entries at the default size; byte/time bounds still apply.
MAX_REQUESTS = MAX_PAGES + 7  # Bounded room for 100 -> 50 -> ... -> 1 reductions.
MAX_RESPONSE_BYTES = 8_000_000
MAX_OBSERVATION_BYTES = 160_000_000  # Aggregate history only; each response remains capped at 8 MB.
MAX_OBSERVATION_SECONDS = 150
COLLECTIONS = ("turns", "messages", "activities")
IDENTITY_FIELDS = ("sessionId", "conversationId", "activeBranchId", "controller", "settings", "branchMaterialization")


class ResponseTooLarge(RoomError):
    """The transport stopped at its unchanged response-size boundary."""


class HistoryObservationLimit(RoomError):
    """A bounded read stopped without complete evidence; safe diagnostic counters."""

    def __init__(self, limit_kind, *, pages, requests, observed_bytes):
        self.limit_kind = limit_kind
        self.pages = pages
        self.requests = requests
        self.observed_bytes = observed_bytes
        self.limit = {"aggregate_bytes": MAX_OBSERVATION_BYTES,
                      "elapsed_seconds": MAX_OBSERVATION_SECONDS,
                      "pages": MAX_PAGES, "requests": MAX_REQUESTS}[limit_kind]
        super().__init__(
            "Complete native history is required within the bounded observation window "
            f"(limit={limit_kind}:{self.limit}; pages={pages}; requests={requests}; "
            f"observed_bytes={observed_bytes})")


def canonical(value):
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False).encode()
    except (ValueError, TypeError, RecursionError) as exc:
        raise RoomError("Raw native history contains invalid JSON evidence") from exc


def conversation(request, path, *, strict=False):
    """Collect raw pages; strict audits additionally refuse conflicting overlap.

Ordinary reads retain the newest page's overlapping state, as before. Every
reader preserves older activities and rejects missing arrays or identity drift.
The caller constructs the validated session path; this helper only issues GETs.
"""
    result = None
    collections = {name: {} for name in COLLECTIONS}
    seen = set()
    cursor = None
    limit = PAGE_ITEMS
    pages = requests = observed_bytes = 0
    deadline = time.monotonic() + MAX_OBSERVATION_SECONDS

    def bounded_result(limit_kind):
        if strict or result is None:
            raise HistoryObservationLimit(limit_kind, pages=pages, requests=requests,
                                          observed_bytes=observed_bytes)
        return finish(True)

    def finish(truncated):
        result.update({name: list(items.values()) for name, items in collections.items()})
        result["messages"].sort(key=lambda m: m.get("sequence", 0))
        result["history_truncated"] = truncated
        result["hasMoreBefore"] = truncated
        return result

    while pages < MAX_PAGES and requests < MAX_REQUESTS:
        if time.monotonic() >= deadline:
            return bounded_result("elapsed_seconds")
        query = path + "?limit=" + str(limit)
        if cursor is not None:
            query += "&beforeSequence=" + str(cursor)
        requests += 1
        try:
            page = request("GET", query)
        except ResponseTooLarge:
            observed_bytes += MAX_RESPONSE_BYTES + 1
            if observed_bytes >= MAX_OBSERVATION_BYTES:
                return bounded_result("aggregate_bytes")
            if limit == 1:
                raise RoomError("One AO history item exceeds 8 MB; preserve the native result and inspect it without replaying the turn")
            limit = max(1, limit // 2)
            continue  # Same history cursor, smaller read; never a model request.
        pages += 1
        observed_bytes += len(canonical(page))
        if observed_bytes > MAX_OBSERVATION_BYTES:
            return bounded_result("aggregate_bytes")
        if time.monotonic() >= deadline:
            return bounded_result("elapsed_seconds")
        if (not isinstance(page, dict) or any(not isinstance(page.get(k), list) for k in COLLECTIONS)
                or type(page.get("hasMoreBefore")) is not bool):
            raise RoomError("AO requires explicit complete native history arrays in the raw AO response")
        if "history_truncated" in page and page["history_truncated"] is not False:
            raise RoomError("Raw native history contains contradictory truncation evidence")
        if result is not None and any(canonical(page.get(k)) != canonical(result.get(k)) for k in IDENTITY_FIELDS):
            raise RoomError("Native conversation identity changed during bounded history observation")
        for name, target in collections.items():
            identities = set()
            for item in page[name]:
                if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
                    raise RoomError("Raw native history identity is ambiguous")
                key = item["id"]
                if strict and key in target and canonical(target[key]) != canonical(item):
                    raise RoomError("Raw native history contains conflicting " + name + " across pages")
                if key in identities:
                    raise RoomError("Raw native history contains duplicate identities")
                identities.add(key)
                if name == "messages" and (type(item.get("sequence", 0)) is not int or item.get("sequence", 0) < 0):
                    raise RoomError("Raw native history message sequence is ambiguous")
                target.setdefault(key, item)
        if result is None:
            result = dict(page)
        if not page["hasMoreBefore"]:
            return finish(False)
        next_cursor = page.get("oldestSequence")
        if (type(next_cursor) is not int or next_cursor <= 0 or next_cursor in seen
                or (cursor is not None and next_cursor >= cursor)):
            raise RoomError("Native history pagination is incomplete or ambiguous")
        seen.add(next_cursor)
        cursor = next_cursor
    return bounded_result("pages" if pages >= MAX_PAGES else "requests")
