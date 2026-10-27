"""Synthetic helper for the workflow evaluation. Standard library only."""


def summarize(values):
    """Return count, mean, minimum and maximum of a non-empty list of numbers."""
    if not isinstance(values, list) or not values or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in values):
        raise ValueError("values must be a non-empty list of numbers")
    return {"count": len(values), "mean": sum(values) / len(values), "min": min(values), "max": max(values)}
