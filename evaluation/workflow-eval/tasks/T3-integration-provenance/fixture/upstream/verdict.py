"""Upstream verdict module (synthetic handback for the workflow evaluation). Pure; standard library only."""

STATUSES = ("pass", "fail", "pending", "not_run")


class InvalidStatusesError(ValueError):
    """Raised for input that is not a non-empty list of known status strings."""


def summarize_statuses(statuses):
    if not isinstance(statuses, list) or not statuses or any(not isinstance(s, str) or s not in STATUSES for s in statuses):
        raise InvalidStatusesError("statuses must be a non-empty list of pass/fail/pending/not_run")
    counts = {status: statuses.count(status) for status in STATUSES}
    verdict = "FAIL" if counts["fail"] else "INCOMPLETE" if counts["pending"] + counts["not_run"] else "PASS"
    return {"counts": counts, "verdict": verdict}
