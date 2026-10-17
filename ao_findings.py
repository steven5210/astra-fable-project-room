"""Read-only projection of finding-provenance tokens inside existing engineering report fields.

Fable tags review_findings, remaining_gaps and backlog entries with whitespace-free key=value
tokens (the lifecycle task-description grammar). This module projects them read-only into
ao_room_status.review_followups.engineering.findings. A category never changes scope, resolves
a finding, blocks anything or grants implementation or publication authority; an untagged entry
is unknown, never resolved or optional. The projection never writes state and never raises on a
malformed report.
"""
import re

import ao_progress

VERSION = 1
FIELDS = ("review_findings", "remaining_gaps", "backlog")
ORIGINS = ("introduced", "preexisting", "integration")
SCOPES = ("required", "optional")
DISPOSITIONS = ("fixed", "open", "deferred", "decision_needed")
KEYS = ("origin", "scope", "disposition", "invariant", "evidence", "owner")

MAX_ITEMS = 16
MAX_TEXT_CHARS = 160
MAX_TOKEN_CHARS = 64
MAX_RAW_CHARS = 32

TOKEN = re.compile(r"(?:^|\s)(origin|scope|disposition|invariant|evidence|owner)=(\S*)")
VOCABULARIES = {"origin": ORIGINS, "scope": SCOPES, "disposition": DISPOSITIONS}

MEANING = ("Tokens the engineer recorded inside the existing report string fields, read as given: "
           "a category never changes scope, resolves a finding or grants implementation or "
           "publication authority; an untagged entry is unknown, never resolved or optional; "
           "a preexisting or integration issue tagged scope=required stays required work.")


def _tokens(entry):
    """The last-occurrence-wins key=value pairs a report entry carries; empty values are absent."""
    values = {}
    for match in TOKEN.finditer(entry):
        values[match.group(1)] = match.group(2)
    return {key: value for key, value in values.items() if value != ""}


def _free(tokens, key):
    """A free provenance token's redacted, bounded display form; absent is None."""
    value = tokens.get(key)
    return ao_progress._clip(value, MAX_TOKEN_CHARS)[0] if value is not None else None


def _item(field, entry):
    """One string entry's projection: parsed vocabulary keys, free tokens and the stripped text."""
    tokens = _tokens(entry)
    item = {"field": field}
    raws = {}
    for key in ("origin", "scope", "disposition"):
        value = tokens.get(key)
        if value is not None and value not in VOCABULARIES[key]:
            item[key] = "unknown"
            raws[key + "_raw"] = ao_progress._clip(value, MAX_RAW_CHARS)[0]
        else:
            item[key] = value if value is not None else "unknown"
            raws[key + "_raw"] = None
    item.update(raws)
    text = " ".join(TOKEN.sub("", entry).split())
    item.update(invariant=_free(tokens, "invariant"), evidence=_free(tokens, "evidence"),
                owner=_free(tokens, "owner"), tagged=bool(tokens),
                text=ao_progress._clip(text, MAX_TEXT_CHARS)[0])
    return item


def project(report):
    """The read-only findings projection of one parsed engineering report; never raises."""
    report = report if isinstance(report, dict) else {}
    fields = {}
    items, items_total = [], 0
    by_origin = {name: 0 for name in ORIGINS} | {"unknown": 0}
    by_scope = {name: 0 for name in SCOPES} | {"unknown": 0}
    by_disposition = {name: 0 for name in DISPOSITIONS} | {"unknown": 0}
    required_open = preexisting_required_open = unknown_scope_open = decision_needed = 0
    for field in FIELDS:
        entries = report.get(field)
        if field not in report:
            fields[field] = {"present": False, "total": 0, "tagged": 0, "untagged": 0, "malformed": 0}
            continue
        if not isinstance(entries, list):
            # The field itself is the malformed unit: present, but its contents cannot be read.
            fields[field] = {"present": True, "total": 0, "tagged": 0, "untagged": 0, "malformed": 1}
            continue
        tagged = untagged = malformed = 0
        for entry in entries:
            if not isinstance(entry, str):
                malformed += 1
                continue
            items_total += 1
            item = _item(field, entry)
            if item["tagged"]:
                tagged += 1
            else:
                untagged += 1
            by_origin[item["origin"]] += 1
            by_scope[item["scope"]] += 1
            by_disposition[item["disposition"]] += 1
            if item["disposition"] != "fixed":
                if item["scope"] == "required":
                    required_open += 1
                    preexisting_required_open += item["origin"] == "preexisting"
                if item["scope"] == "unknown":
                    unknown_scope_open += 1
            decision_needed += item["disposition"] == "decision_needed"
            if len(items) < MAX_ITEMS:
                items.append(item)
        fields[field] = {"present": True, "total": len(entries), "tagged": tagged,
                         "untagged": untagged, "malformed": malformed}
    return {"version": VERSION,
            "assessment": "reported" if any(field in report for field in FIELDS) else "not_reported",
            "fields": fields,
            "total": sum(field["total"] for field in fields.values()),
            "tagged": sum(field["tagged"] for field in fields.values()),
            "untagged": sum(field["untagged"] for field in fields.values()),
            "malformed": sum(field["malformed"] for field in fields.values()),
            "by_origin": by_origin, "by_scope": by_scope, "by_disposition": by_disposition,
            "required_open": required_open, "preexisting_required_open": preexisting_required_open,
            "unknown_scope_open": unknown_scope_open, "decision_needed": decision_needed,
            "items": items, "items_total": items_total, "truncated": items_total > len(items),
            "meaning": MEANING}
