"""Deterministic scorer for workflow-evaluation result bundles. Standard library only; no model, network or credentials.

A bundle is one attempt (case x variant) exported from saved controller records by export_bundle.py plus the probe checks
from probes.py. The scorer never reads rooms, transcripts or repositories itself; it reads bundles and task definitions.

Metrics per attempt:
  accepted_correctness  true / false / null (undetermined: a required check was not run or unavailable)
  correction_rounds     count of engineer requests with purpose "correction"
  integration_effort    engineer dispatches, judge (reviewer) dispatches and elapsed minutes
  usage                 parent (engineer), worker and judge usage kept separate; unknown is never zero
  elapsed_minutes       from the bundle (first dispatch to last settled request), or null
Failures, stops and skips are preserved as attempts with status != "completed".
"""
import argparse
import hashlib
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
PACKAGE = HERE.parent
SCHEMA = "workflow-eval-bundle/1"
STATUSES = ("completed", "failed", "stopped", "skipped")
CHECK_STATES = ("pass", "fail", "not_run", "unavailable")
COUNTERS = ("inputTokens", "outputTokens", "cachedTokens")


class ScoreError(Exception):
    pass


def load_task(case_id):
    for path in sorted((PACKAGE / "tasks").glob("*/task.json")):
        task = json.loads(path.read_text())
        if task.get("case_id") == case_id:
            return task, path.parent
    raise ScoreError("unknown case_id " + str(case_id))


def unseal(task, task_dir, plaintext_path):
    """Return the sealed expectation after verifying its commitment; refuse a mismatch."""
    commitment = (task_dir / task["sealed_expectation_sha256_file"]).read_text().strip()
    data = pathlib.Path(plaintext_path).read_bytes()
    if hashlib.sha256(data).hexdigest() != commitment:
        raise ScoreError("sealed expectation does not match the commitment for " + task["case_id"])
    plain = json.loads(data)
    if plain.get("case_id") != task["case_id"]:
        raise ScoreError("sealed expectation belongs to another case")
    return plain


def expectation(task, task_dir, unsealed):
    """The expected outcome, the required checks and the checks that must not pass."""
    if task["expected_outcome"] == "sealed":
        if unsealed is None:
            return None
        return {"expected_outcome": unsealed["expected_outcome"], "required": list(unsealed["required_checks"]),
                "forbidden": list(unsealed.get("forbidden_checks_pass", []))}
    required = [name for name in task["checks"] if name != "acceptance_not_recorded"] if task["expected_outcome"] == "accepted" \
        else [name for name in task["checks"]]
    return {"expected_outcome": task["expected_outcome"], "required": required, "forbidden": []}


def validate_bundle(bundle):
    if bundle.get("schema") != SCHEMA:
        raise ScoreError("unsupported bundle schema")
    for key in ("case_id", "variant", "attempt", "status", "requests", "checks"):
        if key not in bundle:
            raise ScoreError("bundle lacks " + key)
    if bundle["status"] not in STATUSES:
        raise ScoreError("bundle status must be one of " + ", ".join(STATUSES))
    if not isinstance(bundle["requests"], list) or not isinstance(bundle["checks"], dict):
        raise ScoreError("bundle requests must be a list and checks an object")
    for name, value in bundle["checks"].items():
        if value not in CHECK_STATES:
            raise ScoreError("check " + name + " has state " + str(value) + "; expected one of " + ", ".join(CHECK_STATES))
    for request in bundle["requests"]:
        if request.get("role") not in ("engineer", "reviewer"):
            raise ScoreError("request role must be engineer or reviewer")
        usage = request.get("usage")
        if not isinstance(usage, dict) or "known" not in usage:
            raise ScoreError("request usage must be an object with 'known'")


def usage_totals(items):
    """Sum known counters; count unknown items separately. An unknown item never contributes zero."""
    totals = {counter: 0 for counter in COUNTERS}
    known = unknown = 0
    for usage in items:
        if usage.get("known"):
            known += 1
            for counter in COUNTERS:
                value = usage.get(counter)
                if type(value) is not int or value < 0:
                    raise ScoreError("known usage must carry non-negative integer counters")
                totals[counter] += value
        else:
            unknown += 1
    return {**totals, "known_items": known, "unknown_items": unknown, "complete": unknown == 0}


def accepted_correctness(bundle, expected):
    if expected is None:
        return None, "expectation sealed; unseal to score"
    checks = bundle["checks"]
    if bundle["status"] != "completed":
        return False, "attempt did not complete (" + bundle["status"] + ")"
    if expected["expected_outcome"] == "accepted":
        # Correctness requires the acceptance to name the candidate this bundle exports: an
        # unbound acceptance is undetermined, never true, regardless of the check table.
        acceptance = bundle.get("acceptance") or {}
        verification = bundle.get("verification") or {}
        if not acceptance.get("recorded"):
            return False, "no recorded acceptance of a passed verification"
        if not verification.get("passed") or acceptance.get("bound_to_candidate") is not True:
            return None, "acceptance not bound to the exported candidate: " + str(acceptance.get("unbound_reason"))
    states = {name: checks.get(name, "not_run") for name in expected["required"]}
    if any(state == "fail" for state in states.values()):
        return False, "failed checks: " + ", ".join(sorted(n for n, s in states.items() if s == "fail"))
    if any(checks.get(name) == "pass" for name in expected["forbidden"]):
        return False, "forbidden outcome observed: " + ", ".join(n for n in expected["forbidden"] if checks.get(n) == "pass")
    if any(state != "pass" for state in states.values()):
        return None, "undetermined; not run or unavailable: " + ", ".join(sorted(n for n, s in states.items() if s != "pass"))
    return True, "all required checks pass"


def score_bundle(bundle, unsealed=None):
    validate_bundle(bundle)
    task, task_dir = load_task(bundle["case_id"])
    plain = unseal(task, task_dir, unsealed) if (task["expected_outcome"] == "sealed" and unsealed) else None
    expected = expectation(task, task_dir, plain)
    correct, reason = accepted_correctness(bundle, expected)
    engineer = [r for r in bundle["requests"] if r["role"] == "engineer"]
    reviewer = [r for r in bundle["requests"] if r["role"] == "reviewer"]
    workers = [launch for r in engineer for launch in (r.get("worker_launches") or [])]
    worker_usage = [launch.get("usage") or {"known": False, "reason": "worker usage not exposed"} for launch in workers]
    return {"case_id": bundle["case_id"], "variant": bundle["variant"], "attempt": bundle["attempt"], "status": bundle["status"],
            "stop_reason": bundle.get("stop_reason"), "expected_outcome": expected["expected_outcome"] if expected else "sealed",
            "accepted_correctness": correct, "reason": reason,
            "correction_rounds": sum(1 for r in engineer if r.get("purpose") == "correction"),
            "integration_effort": {"engineer_dispatches": len(engineer), "judge_dispatches": len(reviewer),
                                   "worker_launches": len(workers), "elapsed_minutes": bundle.get("elapsed_minutes")},
            "usage": {"parent": usage_totals([r["usage"] for r in engineer]), "judge": usage_totals([r["usage"] for r in reviewer]),
                      "worker": usage_totals(worker_usage),
                      "note": "AO-reported counters; cached tokens combine reads and writes; not billing or quota"},
            "elapsed_minutes": bundle.get("elapsed_minutes"), "checks": dict(sorted(bundle["checks"].items())),
            "outcomes": {"accepted_candidate": (bundle.get("acceptance") or {}).get("bound_to_candidate") is True
                        and (bundle.get("verification") or {}).get("passed") is True,
                        "engineering_requests_completed": sum(1 for r in engineer
                        if r.get("purpose") in ("implementation", "correction") and r.get("state") == "completed"),
                        "engineering_requests_not_completed": sum(1 for r in engineer
                        if r.get("purpose") in ("implementation", "correction") and r.get("state") != "completed"),
                        "tasks": "unavailable: bundles carry no plan or transcript"}}


def aggregate(scores):
    """Per-variant counts only. n is tiny by design; no significance or speedup claim is derived here."""
    result = {}
    for score in scores:
        if score["expected_outcome"] == "sealed":
            continue  # a held-out case is reported per attempt only until it is unsealed
        bucket = result.setdefault(score["variant"], {"attempts": 0, "completed": 0, "correct": 0, "incorrect": 0, "undetermined": 0,
                                                       "accepted_candidates": 0, "engineering_requests_completed": 0,
                                                       "engineering_requests_not_completed": 0, "correction_rounds": 0, "engineer_dispatches": 0, "judge_dispatches": 0,
                                                       "worker_launches": 0, "elapsed_minutes_known": 0.0, "elapsed_unknown": 0,
                                                       "usage": {role: {c: 0 for c in COUNTERS} | {"unknown_items": 0} for role in ("parent", "worker", "judge")}})
        bucket["attempts"] += 1
        bucket["completed"] += score["status"] == "completed"
        key = {True: "correct", False: "incorrect", None: "undetermined"}[score["accepted_correctness"]]
        bucket[key] += 1
        bucket["accepted_candidates"] += score["outcomes"]["accepted_candidate"] is True
        bucket["engineering_requests_completed"] += score["outcomes"]["engineering_requests_completed"]
        bucket["engineering_requests_not_completed"] += score["outcomes"]["engineering_requests_not_completed"]
        bucket["correction_rounds"] += score["correction_rounds"]
        effort = score["integration_effort"]
        for name in ("engineer_dispatches", "judge_dispatches", "worker_launches"):
            bucket[name] += effort[name]
        if isinstance(score["elapsed_minutes"], (int, float)):
            bucket["elapsed_minutes_known"] += float(score["elapsed_minutes"])
        else:
            bucket["elapsed_unknown"] += 1
        for role in ("parent", "worker", "judge"):
            for counter in COUNTERS:
                bucket["usage"][role][counter] += score["usage"][role][counter]
            bucket["usage"][role]["unknown_items"] += score["usage"][role]["unknown_items"]
    for bucket in result.values():
        bucket["usage_complete"] = all(bucket["usage"][role]["unknown_items"] == 0 for role in ("parent", "worker", "judge"))
    return result


def capacity(scores, caps):
    """Used, retired and remaining capacity against caps.json, overall and per attempt.

    Consumed capacity on a failed, stopped or skipped attempt is retired, never refunded.
    Unknown usage keeps a token figure unknown rather than zero. caps come from caps.json
    only; nothing inside a bundle, score or report can change them.
    """
    per_caps = caps["per_attempt"]
    total_caps = caps["totals"]
    token_caps = per_caps["tokens"]

    completed_scores = [s for s in scores if s["status"] == "completed"]
    retired_scores = [s for s in scores if s["status"] in ("failed", "stopped", "skipped")]

    def effort(score, key):
        return score["integration_effort"][key]

    parent_used = sum(effort(s, "engineer_dispatches") for s in completed_scores)
    parent_retired = sum(effort(s, "engineer_dispatches") for s in retired_scores)
    judge_used = sum(effort(s, "judge_dispatches") for s in completed_scores)
    judge_retired = sum(effort(s, "judge_dispatches") for s in retired_scores)
    worker_used = sum(effort(s, "worker_launches") for s in completed_scores)
    worker_retired = sum(effort(s, "worker_launches") for s in retired_scores)

    def figure(used, retired, cap):
        remaining = cap - used - retired
        out = {"used": used, "retired": retired, "cap": cap, "remaining": max(0, remaining)}
        if remaining < 0:
            out["exceeded"] = True
        return out

    def token_used(group, role):
        return sum(s["usage"][role]["outputTokens"] for s in group)

    def token_figure(cap, role):
        used = token_used(completed_scores, role)
        retired = token_used(retired_scores, role)
        unknown = sum(s["usage"][role]["unknown_items"] for s in scores)
        out = {"used": used, "retired": retired, "cap": cap}
        if unknown:
            out["remaining"] = None
            out["reason"] = "usage unknown for %d items" % unknown
        else:
            remaining = cap - used - retired
            out["remaining"] = max(0, remaining)
            if remaining < 0:
                out["exceeded"] = True
        return out

    def within(score):
        effort = score["integration_effort"]
        parent_out = score["usage"]["parent"]
        judge_out = score["usage"]["judge"]
        return {"parent_dispatches": effort["engineer_dispatches"] <= per_caps["parent_dispatches_max"],
                "judge_dispatches": effort["judge_dispatches"] <= per_caps["judge_dispatches_max"],
                "worker_launches": effort["worker_launches"] <= per_caps["native_worker_launches_max"],
                "parent_output_tokens": (parent_out["outputTokens"] <= token_caps["parent_output_max"]
                                         if parent_out["unknown_items"] == 0 else None),
                "judge_output_tokens": (judge_out["outputTokens"] <= token_caps["judge_output_max"]
                                        if judge_out["unknown_items"] == 0 else None)}

    return {"basis": "caps.json only; no bundle, score or report field can change the caps",
            "overall": {"parent_dispatches": figure(parent_used, parent_retired, total_caps["parent_dispatches_max"]),
                        "judge_dispatches": figure(judge_used, judge_retired, total_caps["judge_dispatches_max"]),
                        "native_worker_launches": figure(worker_used, worker_retired, total_caps["native_worker_launches_max"]),
                        "parent_output_tokens": token_figure(total_caps["parent_output_tokens_max"], "parent"),
                        "judge_output_tokens": token_figure(total_caps["judge_output_tokens_max"], "judge")},
            "per_attempt": [{"case_id": s["case_id"], "variant": s["variant"], "attempt": s["attempt"],
                             "status": s["status"], "within_per_attempt_caps": within(s)} for s in scores],
            "worker_note": "worker output tokens are never measured here; worker usage stays unknown",
            "cached_note": "cached tokens are recorded, never a quota or a cap"}


def markdown(scores, totals, capacity_report=None):
    lines = ["| case | variant | attempt | status | expected | correct | corrections | eng/judge/worker dispatches | elapsed min | parent out | judge out | unknown usage items |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for s in scores:
        effort = s["integration_effort"]
        unknown = sum(s["usage"][role]["unknown_items"] for role in ("parent", "worker", "judge"))
        lines.append("| {case_id} | {variant} | {attempt} | {status} | {expected_outcome} | {correct} | {correction_rounds} | {e}/{j}/{w} | {elapsed} | {po} | {jo} | {unknown} |".format(
            correct={True: "yes", False: "no", None: "undetermined"}[s["accepted_correctness"]], e=effort["engineer_dispatches"],
            j=effort["judge_dispatches"], w=effort["worker_launches"], elapsed=s["elapsed_minutes"] if s["elapsed_minutes"] is not None else "unknown",
            po=s["usage"]["parent"]["outputTokens"] if s["usage"]["parent"]["complete"] else "incomplete",
            jo=s["usage"]["judge"]["outputTokens"] if s["usage"]["judge"]["complete"] else "incomplete", unknown=unknown, **s))
    lines.append("")
    lines.append("| variant | attempts | completed | correct | incorrect | undetermined | corrections | engineer dispatches | elapsed min (known) | usage complete |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for variant, b in sorted(totals.items()):
        lines.append(f"| {variant} | {b['attempts']} | {b['completed']} | {b['correct']} | {b['incorrect']} | {b['undetermined']} | {b['correction_rounds']} | {b['engineer_dispatches']} | {round(b['elapsed_minutes_known'], 1)} (unknown: {b['elapsed_unknown']}) | {'yes' if b['usage_complete'] else 'no'} |")
    lines.append("")
    lines.append("## Outcomes")
    lines.append("| case | variant | attempt | accepted candidate | engineering completed | engineering not completed |")
    lines.append("|---|---|---|---|---|---|")
    for s in scores:
        outcomes = s["outcomes"]
        lines.append("| {case_id} | {variant} | {attempt} | {a} | {c} | {n} |".format(
            a="yes" if outcomes["accepted_candidate"] else "no",
            c=outcomes["engineering_requests_completed"],
            n=outcomes["engineering_requests_not_completed"], **s))
    lines.append("Tasks are unavailable: bundles carry no plan or transcript.")
    lines.append("")
    lines.append("| variant | accepted candidates | engineering completed | engineering not completed |")
    lines.append("|---|---|---|---|")
    for variant, b in sorted(totals.items()):
        lines.append(f"| {variant} | {b['accepted_candidates']} | {b['engineering_requests_completed']} | {b['engineering_requests_not_completed']} |")
    if capacity_report is not None:
        lines.append("")
        lines.append("## Capacity")
        lines.append("| counter | used | retired | remaining | cap |")
        lines.append("|---|---|---|---|---|")
        for name, figure in sorted(capacity_report["overall"].items()):
            remaining = figure["remaining"]
            lines.append(f"| {name} | {figure['used']} | {figure['retired']} | "
                         f"{remaining if remaining is not None else figure.get('reason', 'unknown')} | {figure['cap']} |")
        lines.append("Used is completed attempts only; retired is failed, stopped or skipped attempts and is never refunded.")
    lines.append("")
    lines.append("Small sample: at most three cases per variant. Counts are not rates. Overlap or elapsed time alone is not a speedup claim. Unknown usage is not zero.")
    return "\n".join(lines)


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("bundles", nargs="+", help="bundle JSON files")
    parser.add_argument("--unseal", help="plaintext sealed expectation for the held-out case (verified against the commitment)")
    parser.add_argument("--caps", help="caps JSON for the capacity figures (default the package's caps.json)")
    parser.add_argument("--json", action="store_true", help="print JSON instead of Markdown")
    args = parser.parse_args(argv)
    scores = []
    for path in args.bundles:
        bundle = json.loads(pathlib.Path(path).read_text())
        scores.append(score_bundle(bundle, args.unseal))
    scores.sort(key=lambda s: (s["case_id"], s["variant"], s["attempt"]))
    totals = aggregate(scores)
    caps_path = pathlib.Path(args.caps) if args.caps else PACKAGE / "caps.json"
    capacity_report = capacity(scores, json.loads(caps_path.read_text()))
    if args.json:
        print(json.dumps({"scores": scores, "per_variant": totals, "capacity": capacity_report}, indent=2, sort_keys=True))
    else:
        print(markdown(scores, totals, capacity_report))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except ScoreError as exc:
        sys.stderr.write("score: " + str(exc) + "\n")
        sys.exit(2)
