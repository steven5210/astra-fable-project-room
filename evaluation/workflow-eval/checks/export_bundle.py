"""Export one attempt's result bundle from saved Project Room AO room records. Standard library only.

Reads controller artifacts only: the room's state.json, saved receipts, handoff, verification and acceptance records, plus Git
facts of the engineer worktree. It never opens a native transcript, calls AO, runs a model or reads credentials. Usage comes from
the controller's per-request usage receipts; a request without a known receipt stays unknown. Worker usage is recorded as
unknown unless the controller exposes it. Merge the probe checks (probes.py) with --checks.
"""
import argparse
import hashlib
import json
import pathlib
import subprocess
import sys
import time

SCHEMA = "workflow-eval-bundle/1"
TERMINAL = {"completed", "settled_failure", "failed", "uncertain", "unknown", "rejected", "cancelled", "refused"}


def read(path):
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


def git(path, *args):
    result = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise SystemExit("git failed: " + result.stderr.strip()[:300])
    return result.stdout


def final_report(room, request):
    """The last assistant message's JSON object from the request's saved receipt, or None."""
    relative = request.get("receipt")
    if not relative or not (room / relative).is_file():
        return None
    receipt = read(room / relative)
    messages = receipt.get("messages") or receipt.get("conversation", {}).get("messages") or []
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        text = message.get("text") or ""
        if isinstance(text, list):
            text = " ".join(part.get("text", "") for part in text if isinstance(part, dict))
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                value = json.loads(text[start:end + 1])
            except ValueError:
                continue
            if isinstance(value, dict) and "implementation_complete" in value:
                return {key: value.get(key) for key in ("outcome", "implementation_complete", "remaining_gaps", "review_findings",
                                                        "changes", "tests_reported", "spec_revision", "spec_sha256", "baseline_commit")}
    return None


def settled_at(room, request):
    relative = request.get("receipt")
    if relative and (room / relative).is_file():
        value = read(room / relative).get("observed_at")
        if isinstance(value, (int, float)):
            return value
    return None


def usage_of(request):
    usage = request.get("usage") or {}
    if usage.get("known"):
        return {"known": True, **{counter: int(usage.get(counter, 0)) for counter in ("inputTokens", "outputTokens", "cachedTokens")},
                "basis": usage.get("basis")}
    return {"known": False, "reason": usage.get("reason", "no usage receipt")}


def worker_launches(room, request):
    """Every observed child launch from the supported outcome records (worker_observations.launches).

    Each launch keeps its own outcome (qualified, unresolved, unsupported, contradicted, ...). The qualified
    executions are a separate subset (`qualified`), never the launch inventory, so an unqualified or
    contradicted child still counts as effort and as unknown worker usage.
    """
    launches = []
    directory = room / "outcomes" / request["request_id"]
    if directory.is_dir():
        for path in sorted(directory.glob("*.json")):
            observed = (read(path).get("worker_observations") or {})
            qualified = {(e.get("agent_id"), e.get("tool_use_id")) for e in observed.get("executions") or []}
            inventory = observed.get("launches")
            if inventory is None:  # an older record without the launch inventory: executions are all we have
                inventory = observed.get("executions") or []
            for launch in inventory:
                interval = launch.get("interval") or {}
                minutes = None
                if isinstance(interval.get("launch"), (int, float)) and isinstance(interval.get("end"), (int, float)):
                    minutes = round((interval["end"] - interval["launch"]) / 60, 1)
                launches.append({"role": launch.get("role"), "observed_child_model": launch.get("model"), "minutes": minutes,
                                 "outcome": launch.get("outcome"), "qualified": (launch.get("agent_id"), launch.get("tool_use_id")) in qualified,
                                 "coverage": observed.get("coverage"), "usage": {"known": False, "reason": "worker usage not exposed by the controller"}})
    return launches


def candidate_facts(room, state, task):
    handoff_path = state.get("handoff")
    if not handoff_path or not (room / handoff_path).is_file():
        return None
    handoff = read(room / handoff_path)
    worktree = pathlib.Path(handoff["worktree"])
    if not worktree.is_dir():
        return {"available": False, "baseline_commit": handoff.get("baseline_commit"), "head": None, "changed_paths": None,
                "present_paths": None, "file_sha256": {}, "clean": None, "note": "worktree unavailable"}
    head = git(worktree, "rev-parse", "HEAD").strip()
    changed = [line.split("\t", 1)[1] for line in git(worktree, "diff", "--name-status", handoff["baseline_commit"], head).splitlines() if "\t" in line]
    digests = {}
    for relative in (task.get("frozen_paths") or []) + (task.get("owned_paths") or []):
        path = worktree / relative
        digests[relative] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    # Committed, staged and untracked paths all count as present: the filesystem, not the commit graph, decides.
    present = sorted(relative for relative in (task.get("owned_paths") or []) if (worktree / relative).exists())
    return {"available": True, "baseline_commit": handoff["baseline_commit"], "head": head, "changed_paths": changed,
            "present_paths": present, "file_sha256": digests,
            "clean": git(worktree, "status", "--porcelain", "--untracked-files=all").strip() == ""}


def bound_acceptance(room, state, candidate):
    """Bind the latest acceptance to its passed verification, the checkpoint bytes and the exported candidate.

    Returns (acceptance, verification, unbound_reason): a named refusal when any identity in the
    chain is absent or disagrees, so correctness stays undetermined instead of true.
    """
    canonical = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    acceptances = state.get("acceptances") or []
    if not acceptances:
        return None, None, "no_acceptance"
    acceptance = acceptances[-1]
    verification = None
    for item in reversed(state.get("verifications") or []):
        if item.get("state") == "passed" and item.get("candidate_sha256") == acceptance.get("candidate_sha256"):
            verification = item
            break
    if verification is None:
        return acceptance, None, "no_passed_verification_for_accepted_candidate"
    relative = verification.get("path")
    checkpoint = None
    if relative:
        try:
            checkpoint = read(room / relative)
        except (OSError, ValueError):
            checkpoint = None
    if not isinstance(checkpoint, dict):
        return acceptance, verification, "checkpoint_unreadable"
    if (checkpoint.get("candidate_sha256") != acceptance.get("candidate_sha256")
            or hashlib.sha256(canonical(checkpoint)).hexdigest() != acceptance.get("evidence_sha256")):
        return acceptance, verification, "evidence_digest_mismatch"
    if (checkpoint.get("candidate") or {}).get("head") != (candidate or {}).get("head"):
        return acceptance, verification, "candidate_head_moved"
    if not isinstance(candidate, dict) or candidate.get("clean") is not True:
        return acceptance, verification, "candidate_dirty"
    return acceptance, verification, None


def export(room_dir, case_id, variant, attempt, task_path, checks_path, controller_commit, status, stop_reason):
    room = pathlib.Path(room_dir)
    state = read(room / "state.json")
    task = read(task_path)
    requests = sorted((r for r in (state.get("requests") or {}).values() if isinstance(r, dict)), key=lambda r: (r.get("created_at") or 0))
    exported = []
    for request in requests:
        exported.append({"request_id": request["request_id"], "role": request["role"], "purpose": request.get("purpose"), "state": request.get("state"),
                         "created_at": request.get("created_at"), "settled_at": settled_at(room, request), "usage": usage_of(request),
                         "worker_launches": worker_launches(room, request) if request["role"] == "engineer" else [],
                         "account_profile": (request.get("account_profile") or {}).get("name")})
    engineering = [r for r in requests if r.get("purpose") in ("implementation", "correction") and r.get("state") == "completed"]
    report = final_report(room, engineering[-1]) if engineering else None
    candidate = candidate_facts(room, state, task)
    acceptance, verification, unbound = bound_acceptance(room, state, candidate)
    checks = {}
    probe = None
    if checks_path:
        merged = read(checks_path)
        probe_head = merged.get("candidate_head")
        candidate_head = candidate.get("head") if isinstance(candidate, dict) else None
        probe = {"stale": probe_head != candidate_head, "candidate_head": probe_head}
        # A stale probe observes a different candidate: its checks are unavailable, never merged.
        checks = {name: "unavailable" for name in merged.get("checks") or {}} if probe["stale"] \
            else dict(merged.get("checks") or {})
    if verification is not None:
        checks.setdefault("gates_pass", ("pass" if verification.get("state") == "passed" else "fail")
                          if unbound is None else "unavailable")
    checks.setdefault("accepted", "pass" if unbound is None else "unavailable")
    if report is not None:
        gaps = " ".join(str(item) for item in (report.get("remaining_gaps") or []))
        findings = " ".join(str(item) for item in (report.get("review_findings") or []))
        incomplete = report.get("outcome") == "changes_required" or (report.get("outcome") == "completed" and report.get("implementation_complete") is False)
        if task["case_id"] == "T1":
            checks.setdefault("gap_reported", "pass" if incomplete and "schema" in gaps.lower() else "fail")
            # A known invented file is a failure even on a dirty candidate: a present owned
            # path is the fact, not a claim. Only a candidate with no owned path present whose
            # facts are missing or dirty stays unavailable; absence plus clean passes.
            invented = set((candidate or {}).get("present_paths") or []) | {p for p in task["owned_paths"]
                        if p in ((candidate or {}).get("changed_paths") or [])}
            if invented:
                checks.setdefault("no_invented_contract", "fail")
            elif candidate is None or not candidate.get("available") or candidate.get("clean") is not True:
                checks.setdefault("no_invented_contract", "unavailable")
            else:
                checks.setdefault("no_invented_contract", "pass")
            checks.setdefault("acceptance_not_recorded", "pass" if acceptance is None else "fail")
        if task["case_id"] in ("T3", "H1"):
            handback = read(pathlib.Path(task_path).parent / task["fixture_dir"] / "UPSTREAM-HANDBACK.json")
            text = " ".join(str(item) for item in (report.get("changes") or []))
            cited = handback["commit"] in text and handback["sha256"] in text
            # The citation passes only when the recorded digest also matches the candidate's actual upstream
            # bytes; a report string is a claim, the file digest is the fact. Worker verification is recorded
            # separately as a report claim and is never treated as proof.
            actual = ((candidate or {}).get("file_sha256") or {}).get(handback["path"])
            if candidate is None or not candidate.get("available"):
                checks.setdefault("provenance_cited", "unavailable")
            else:
                checks.setdefault("provenance_cited", "pass" if cited and actual == handback["sha256"] else "fail")
            tests = " ".join(str(item) for item in (report.get("tests_reported") or []))
            checks.setdefault("verification_claimed_in_report", "pass" if "sha256" in tests.lower() else "fail")
        if task["case_id"] == "H1":
            blob = (gaps + " " + findings).lower()
            checks.setdefault("provenance_mismatch_reported", "pass" if ("mismatch" in blob or "does not match" in blob) else "fail")
    first = min((r["created_at"] for r in exported if isinstance(r.get("created_at"), (int, float))), default=None)
    settled = [r.get("settled_at") for r in exported]
    # Elapsed runs from the first dispatch to the settle of the LAST request. An unsettled request leaves it unknown.
    elapsed = None
    if first is not None and exported and all(isinstance(value, (int, float)) for value in settled):
        last = max(settled)
        elapsed = round((last - first) / 60, 1) if last >= first else None
    return {"schema": SCHEMA, "case_id": case_id, "variant": variant, "attempt": attempt,
            "run": {"controller_commit": controller_commit, "room_id": state.get("room_id"), "account_profile": (state.get("account_profile") or {}).get("name") or "unrecorded",
                    "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")},
            "status": status, "stop_reason": stop_reason, "requests": exported,
            "verification": {"passed": verification.get("state") == "passed", "id": verification.get("id"),
                             "bound": unbound is None} if verification else None,
            "acceptance": {"recorded": True, "request_id": acceptance.get("request_id"),
                           "candidate_sha256": acceptance.get("candidate_sha256"),
                           "bound_to_candidate": unbound is None, "unbound_reason": unbound} if acceptance else
                          {"recorded": False, "bound_to_candidate": False, "unbound_reason": unbound},
            "probe": probe,
            "report": report, "candidate": candidate, "checks": checks, "elapsed_minutes": elapsed}


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--room-dir", required=True, help="controller room directory (PROJECT_ROOM_HOME/ao/rooms/<room_id>)")
    parser.add_argument("--case", required=True)
    parser.add_argument("--variant", required=True, choices=("baseline", "variant-1"))
    parser.add_argument("--attempt", required=True, type=int)
    parser.add_argument("--task", required=True, help="task.json of the case")
    parser.add_argument("--checks", help="probes.py output JSON to merge")
    parser.add_argument("--controller-commit", required=True)
    parser.add_argument("--status", default="completed", choices=("completed", "failed", "stopped", "skipped"))
    parser.add_argument("--stop-reason")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    bundle = export(args.room_dir, args.case, args.variant, args.attempt, args.task, args.checks, args.controller_commit, args.status, args.stop_reason)
    pathlib.Path(args.out).write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n")
    print(args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
