"""Explicit, read-only attribution of one owned request's native and API usage."""

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat

import ao_evidence_audit as evidence
import ao_evidence_audit_native as native
import deepseek_adapter


AuditError = evidence.AuditError
LIMITATIONS = ("not_billing_or_quota", "single_request_not_workflow_total", "configured_identity_not_attested",
               "compaction_calls_not_logged", "bounded_time_correlation", "delegate_selection_by_time_window")
COUNTER_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
NON_INCOMPLETE_REASONS = frozenset(("cache_split_unavailable", "configured_identity_unknown",
                                    "context_attachment_file", "context_attachment_unclassified"))
NON_INCOMPLETE_DELEGATE_REASONS = frozenset(("delegate_usage_non_final_chunk",))
MAX_DELEGATE_ROWS = 10000
MAX_USAGE_JSON_CHARS = 65536


def _empty_counters(value=None):
    return dict.fromkeys(COUNTER_KEYS, value)


def _add_counters(left, right):
    return {key: None if left[key] is None or right[key] is None else left[key] + right[key]
            for key in COUNTER_KEYS}


def _sum_counters(values):
    result = _empty_counters(0)
    for value in values:
        result = _add_counters(result, value)
    return result


def _marker_in_interval(interval, number, timestamp):
    if timestamp is not None:
        return interval.contains(number, timestamp)
    # Record numbers are actor-local: only a parent Interval can place a marker by number. A child
    # interval is bounded by timestamps alone, so a marker without one cannot be excluded (fail closed).
    return interval.contains_number(number) if isinstance(interval, native.Interval) else True


def _actor_unavailable(configured_model, reasons):
    return {"coverage": "unavailable", "cache_coverage": "unavailable", "configured_model": configured_model,
            "attested_models": [], "responses": None, "repeated_response_records": None,
            "synthetic_responses": None, "counters": _empty_counters(), "reasons": sorted(set(reasons))}


def _actor(scan, interval, configured_model, reasons=(), excluded_ids=()):
    reasons = set(reasons)
    if interval is None or scan is None:
        return _actor_unavailable(configured_model, reasons), None
    reasons.update(scan.notes & (native.SOURCE_DIMENSION | native.INTERVAL_DIMENSION))
    observations = [item for item in scan.usage_observations if interval.contains(item[0], item[1])]
    reasons.update(reason for number, timestamp, reason in scan.usage_reasons
                   if interval.contains(number, timestamp))
    if any(_marker_in_interval(interval, number, timestamp) for number, timestamp in scan.compaction_markers):
        reasons.add("compaction_in_interval")
    groups = {}
    for observation in observations:
        groups.setdefault(observation[2], []).append(observation)
    counters = _empty_counters(0)
    responses = 0
    repeated = 0
    synthetic = 0
    attested_models = set()
    counters_by_model = {}
    for message_id, entries in groups.items():
        if message_id in excluded_ids:
            reasons.add("usage_conflict")
            continue
        models = {entry[3] for entry in entries}
        usages = [entry[4] for entry in entries]
        if len(models) != 1:
            reasons.add("usage_conflict")
            continue
        model = entries[0][3]
        if model == "<synthetic>":
            synthetic += 1
            repeated += len(entries) - 1
            continue
        if any(usage is None for usage in usages):
            continue
        input_cache = {(usage[0], usage[2], usage[3]) for usage in usages}
        if len(input_cache) != 1:
            reasons.add("usage_conflict")
            continue
        output = max(usage[1] for usage in usages)
        repeated += len(entries) - 1
        responses += 1
        attested_models.add(model)
        response = {"input_tokens": entries[0][4][0], "output_tokens": output,
                    "cache_creation_input_tokens": entries[0][4][2],
                    "cache_read_input_tokens": entries[0][4][3]}
        counters = _add_counters(counters, response)
        counters_by_model[model] = _add_counters(counters_by_model.get(model, _empty_counters(0)), response)
        if response["cache_creation_input_tokens"] is None or response["cache_read_input_tokens"] is None:
            reasons.add("cache_split_unavailable")
    if configured_model is None:
        reasons.add("configured_identity_unknown")
    token_reasons = reasons - NON_INCOMPLETE_REASONS
    coverage = "complete" if not token_reasons else "incomplete"
    cache_coverage = "incomplete" if coverage != "complete" or "cache_split_unavailable" in reasons else "complete"
    return ({"coverage": coverage, "cache_coverage": cache_coverage, "configured_model": configured_model,
             "attested_models": sorted(attested_models), "responses": responses,
             "repeated_response_records": repeated, "synthetic_responses": synthetic,
             "counters": counters, "reasons": sorted(reasons)}, counters_by_model)


def _job_usage(value):
    try:
        parsed = json.loads(value) if isinstance(value, str) else None
    except (ValueError, TypeError, RecursionError):
        return None
    if not isinstance(parsed, dict):
        return None
    cleaned = deepseek_adapter._clean_usage(parsed)
    if cleaned is None:
        return None
    allowed = set(deepseek_adapter.USAGE_FIELDS) | set(deepseek_adapter.USAGE_DETAIL_FIELDS)
    return {key: item for key, item in cleaned.items() if key in allowed}


def _api_delegates(home, state, request, receipt, room):
    delegate = state.get("delegate")
    if not isinstance(delegate, dict) or delegate.get("provider") != "deepseek":
        return {"coverage": "not_applicable"}
    path = Path(home) / "deepseek" / "ledger.sqlite3"
    try:
        ledger_stat = os.lstat(path)
    except FileNotFoundError:
        return {"coverage": "unavailable", "reason": "delegate_ledger_absent"}
    except (OSError, ValueError):
        return {"coverage": "unavailable", "reason": "delegate_ledger_unreadable"}
    if not stat.S_ISREG(ledger_stat.st_mode):
        return {"coverage": "unavailable", "reason": "delegate_ledger_unsafe"}
    jobs = []
    reasons = set()
    try:
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            cursor = connection.execute(
                "SELECT substr(id,1,1024), substr(requested_model,1,256), substr(observed_model,1,256), "
                "CASE WHEN length(usage_json) > ? THEN NULL ELSE usage_json END, "
                "substr(usage_source,1,256), substr(state,1,256), substr(created_at,1,256), "
                "length(usage_json) > ? AS usage_oversized "
                "FROM jobs WHERE room_id=? ORDER BY rowid LIMIT ?",
                (MAX_USAGE_JSON_CHARS, MAX_USAGE_JSON_CHARS, room, MAX_DELEGATE_ROWS + 1))
            rows = []
            for row in cursor:
                rows.append(row)
                if len(rows) > MAX_DELEGATE_ROWS:
                    return {"coverage": "unavailable", "reason": "delegate_ledger_limit"}
        finally:
            connection.close()
    except (sqlite3.Error, OSError, ValueError):
        return {"coverage": "unavailable", "reason": "delegate_ledger_unreadable"}
    start = (request["created_at"] if native.finite_seconds(request.get("created_at"))
             else native.parse_timestamp(request.get("created_at")))
    end = (receipt["observed_at"] if native.finite_seconds(receipt.get("observed_at"))
           else native.parse_timestamp(receipt.get("observed_at")))
    selected = []
    for row in rows:
        created_at = native.parse_timestamp(row[6])
        if created_at is None or start is None or end is None:
            reasons.add("delegate_time_unparsable")
            continue
        if start <= created_at <= end:
            selected.append(row)
    for (job_id, requested_model, observed_model, usage_json, usage_source, _state, _created_at,
         usage_oversized) in selected:
        if not native.bounded_text(job_id):
            reasons.add("delegate_job_identity_unavailable")
            continue
        safe_requested_model = requested_model if native.valid_model_id(requested_model) else None
        safe_observed_model = observed_model if native.valid_model_id(observed_model) else None
        if safe_requested_model is None or (observed_model is not None and safe_observed_model is None):
            reasons.add("delegate_model_unavailable")
        if usage_source is not None and not native.bounded_text(usage_source):
            reasons.add("delegate_usage_source_unavailable")
            usage_source = None
        if usage_source not in ("final_chunk", "usage_only_chunk", "content_chunk"):
            reasons.add("delegate_usage_source_unavailable")
            usage_source = None
        elif usage_source == "content_chunk":
            reasons.add("delegate_usage_non_final_chunk")
        if usage_oversized:
            usage = None
        else:
            usage = _job_usage(usage_json)
        if usage is None:
            reasons.add("delegate_usage_unavailable")
        jobs.append({"job_sha256": hashlib.sha256(job_id.encode("utf-8")).hexdigest(),
                     "requested_model": safe_requested_model, "observed_model": safe_observed_model,
                     "usage_source": usage_source, "usage": usage})
    jobs.sort(key=lambda item: item["job_sha256"])
    coverage = "complete" if not (reasons - NON_INCOMPLETE_DELEGATE_REASONS) else "incomplete"
    return {"coverage": coverage, "jobs": jobs, "reasons": sorted(reasons)}


def _configured_model(prepared, configured_agent):
    if configured_agent is None:
        return None
    routing = prepared.get("routing")
    agents = routing.get("agents") if isinstance(routing, dict) else None
    value = agents.get(configured_agent) if isinstance(agents, dict) else None
    return value if native.bounded_text(value) else None


def _configured_agent(prepared, subagent_type):
    routing = prepared.get("routing")
    agents = routing.get("agents") if isinstance(routing, dict) else None
    if isinstance(subagent_type, str) and isinstance(agents, dict) and subagent_type in agents:
        return subagent_type
    return None


def _actor_inputs(gathered, request, prepared):
    actors = []
    parent_scan = gathered["parent_scan"]
    actors.append({"actor_sha256": gathered["parent_actor"],
                   "scan": parent_scan, "interval": gathered["interval"],
                   "configured_model": request.get("model"), "configured_agent": None,
                   "entry": None, "usage": None, "counters_by_model": None})
    for entry in gathered["admitted"]:
        configured_agent = _configured_agent(prepared, entry.get("configured_agent"))
        actors.append({"actor_sha256": entry["actor_sha256"], "scan": entry["scan"],
                       "interval": entry["interval"] if not entry["ambiguous"] else None,
                       "configured_model": _configured_model(prepared, configured_agent),
                       "configured_agent": configured_agent, "entry": entry,
                       "usage": None, "counters_by_model": None})
    return actors


def _group_actors(actors):
    appearances = {}
    for actor in actors:
        scan, interval = actor["scan"], actor["interval"]
        if scan is None or interval is None:
            continue
        for number, timestamp, message_id, model, usage in scan.usage_observations:
            if interval.contains(number, timestamp):
                appearances.setdefault(message_id, set()).add(actor["actor_sha256"])
    conflicts = {message_id for message_id, actor_ids in appearances.items() if len(actor_ids) > 1}
    for actor in actors:
        scan, interval = actor["scan"], actor["interval"]
        reasons = set()
        actor_conflicts = set()
        if actor["entry"] is not None and actor["entry"]["reason"]:
            reasons.add(actor["entry"]["reason"])
        if scan is not None and interval is not None and conflicts:
            actor_conflicts = {item[2] for item in scan.usage_observations
                               if item[2] in conflicts and interval.contains(item[0], item[1])}
        actor["usage"], actor["counters_by_model"] = _actor(
            scan, interval, actor["configured_model"], reasons, actor_conflicts)
    return actors


def _child_coverage(actors, gathered):
    if gathered["parent_unavailable"] or gathered["interval"] is None:
        return "unavailable"
    if (gathered["child_problems"]
            or gathered["notes"] & (native.SOURCE_DIMENSION | native.INTERVAL_DIMENSION)
            or any(actor["usage"]["coverage"] != "complete"
                   for actor in actors[1:])):
        return "incomplete"
    return "complete"


def _native_totals(parent, children, child_coverage, child_counters_by_model):
    primary = parent["counters"] if parent["coverage"] == "complete" else None
    if not children and child_coverage == "complete":
        workers = _empty_counters(0)
    elif (child_coverage == "complete"
          and all(child["usage"] is not None and child["usage"]["coverage"] == "complete" for child in children)):
        workers = _sum_counters(child["usage"]["counters"] for child in children)
    else:
        workers = None
    by_model = None
    if workers is not None:
        by_model = {}
        for counters_by_model in child_counters_by_model:
            for model, counters in counters_by_model.items():
                by_model[model] = _add_counters(by_model.get(model, _empty_counters(0)), counters)
    combined = _add_counters(primary, workers) if primary is not None and workers is not None else None
    return {"coverage": "complete" if combined is not None else "incomplete", "primary": primary,
            "workers": workers, "workers_by_attested_model": by_model, "combined": combined}


def _relation(request, parent, workers):
    usage = request.get("usage")
    if not isinstance(usage, dict) or usage.get("known") is not True or parent["coverage"] != "complete":
        return "unavailable"
    counters = parent["counters"]
    creation, read = counters["cache_creation_input_tokens"], counters["cache_read_input_tokens"]
    if creation is None or read is None:
        return "unavailable"
    fields = (usage.get("inputTokens"), usage.get("outputTokens"), usage.get("cachedTokens"))
    if any(type(value) is not int or value < 0 for value in fields):
        return "unavailable"
    parent_sum = (counters["input_tokens"], counters["output_tokens"], creation + read)
    if fields == parent_sum:
        return "parent_sum"
    if workers is not None:
        if workers["cache_creation_input_tokens"] is None or workers["cache_read_input_tokens"] is None:
            return "unavailable"
        all_sum = (parent_sum[0] + workers["input_tokens"], parent_sum[1] + workers["output_tokens"],
                   parent_sum[2] + workers["cache_creation_input_tokens"] + workers["cache_read_input_tokens"])
        if fields == all_sum:
            return "parent_and_workers_sum"
    return "neither"


def _work_unit(request):
    return {key: request.get(key) for key in ("role", "purpose", "state", "provider_epoch")}


def _report(binding, gathered, parent, child_actors, child_coverage, api_delegates, reasons):
    reasons = set(reasons)
    children = [{"actor_sha256": actor["actor_sha256"],
                 "configured_agent": actor["configured_agent"],
                 "usage": actor["usage"] if actor["usage"]["coverage"] != "unavailable" else None}
                for actor in sorted(child_actors, key=lambda item: item["actor_sha256"])]
    totals = _native_totals(parent, children, child_coverage,
                            [actor["counters_by_model"] for actor in child_actors])
    relation = _relation(binding["request"], parent, totals["workers"])
    reasons.update(parent["reasons"])
    for actor in child_actors:
        reasons.update(actor["usage"]["reasons"])
    if child_coverage != "complete" and child_coverage != "unavailable":
        reasons.update(gathered["child_problems"])
    reasons.update(api_delegates.get("reasons", ()))
    if api_delegates.get("coverage") == "unavailable":
        reasons.add(api_delegates.get("reason"))
    incomplete_reasons = reasons - NON_INCOMPLETE_REASONS - NON_INCOMPLETE_DELEGATE_REASONS
    coverage = ("complete" if totals["coverage"] == "complete"
                and api_delegates["coverage"] in ("complete", "not_applicable")
                and not incomplete_reasons else "incomplete")
    return {"version": 1, "kind": "native_usage", "scope": "single_request", "coverage": coverage,
            "request_sha256": binding["request_sha256"], "owner_sha256": binding["owner_sha256"],
            "work_unit": _work_unit(binding["request"]), "sources": gathered["sources"], "parent": parent,
            "children": children, "child_coverage": child_coverage, "native_totals": totals,
            "parent_rollup": {"basis": "actor_partitioned_records", "parent_counts_include_children": False,
                              "reported_child_summaries_used": False},
            "ao_primary_counter_relation": relation, "api_delegates": api_delegates,
            "reasons": sorted(reasons), "limitations": list(LIMITATIONS)}


def _unavailable(reason, request_sha256=None, owner_sha256=None):
    return {"version": 1, "kind": "native_usage", "scope": "single_request", "coverage": "unavailable",
            "request_sha256": request_sha256, "owner_sha256": owner_sha256,
            "work_unit": {"role": None, "purpose": None, "state": None, "provider_epoch": None},
            "sources": [], "parent": None, "children": [], "child_coverage": "unavailable",
            "native_totals": {"coverage": "unavailable", "primary": None, "workers": None,
                              "workers_by_attested_model": None, "combined": None},
            "parent_rollup": {"basis": "actor_partitioned_records", "parent_counts_include_children": False,
                              "reported_child_summaries_used": False},
            "ao_primary_counter_relation": "unavailable",
            "api_delegates": {"coverage": "unavailable", "reason": "not_evaluated"}, "reasons": [reason],
            "limitations": list(LIMITATIONS)}


def audit(home, room, request_id, database):
    """Return a closed, bounded usage report for one bound owned request."""
    room = evidence._component(room, "room_invalid")
    request_id = evidence._component(request_id, "request_invalid")
    database = evidence._absolute(database, "database_invalid")
    home = evidence._absolute(home, "home_invalid")
    binding = None
    try:
        binding = evidence._bind(home, room, request_id, database, None)
        gathered = evidence._gather(binding)
        try:
            binding["room_root"].recheck_bindings("request_integrity")
        except evidence.audit_io.SourceError:
            raise evidence.AuditRefusal("request_integrity", binding["request_sha256"],
                                        binding["owner_sha256"]) from None
        if gathered["parent_unavailable"]:
            parent, _ = _actor(None, None, binding["request"]["model"], gathered["notes"])
            actors = []
            child_coverage = "unavailable"
        else:
            actors = _group_actors(_actor_inputs(gathered, binding["request"], binding["prepared"]))
            parent = actors[0]["usage"]
            child_actors = actors[1:]
            child_coverage = _child_coverage(actors, gathered)
        if gathered["parent_unavailable"]:
            actors = []
            child_actors = []
        else:
            child_actors = actors[1:]
        api_delegates = _api_delegates(home, binding["state"], binding["request"], binding["receipt"], room)
        reasons = set(gathered["notes"])
        reasons.update(gathered["child_problems"])
        return _report(binding, gathered, parent, child_actors, child_coverage, api_delegates, reasons)
    except evidence.AuditRefusal as refusal:
        return _unavailable(refusal.reason, refusal.request_sha256, refusal.owner_sha256)
    finally:
        if binding is not None:
            binding["room_root"].close()
