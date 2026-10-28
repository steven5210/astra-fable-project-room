"""Offline dry run of the workflow evaluation package. Standard library only; no model, network or credentials.

Steps:
  1. Verify the frozen task inputs against tasks/manifest.json (use --freeze once to write the manifest).
  2. Verify the variant manifest digests and that exactly two variants exist.
  3. Verify the held-out commitment file shape (64 hex) and, with --unseal, that the plaintext matches it.
  4. Verify caps.json shape and totals (cases x variants = 6, no retries, no expansion).
  5. Run the probes on two synthetic T2 candidates: one that exercises the entry point (stub detected) and one helper-only
     candidate (stub not detected), and check the expected probe outcomes.
  6. Score the synthetic result bundles in fixtures/dry-run and compare with the expected per-attempt outcomes.
Exit 0 only when every step passes.
"""
import argparse
import hashlib
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

PACKAGE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE / "checks"))
sys.path.insert(0, str(PACKAGE.parent.parent))
import export_bundle  # noqa: E402
import probes  # noqa: E402
import score  # noqa: E402
import ao_timing  # noqa: E402

EXPECTED_DRY_RUN = {("T1", "baseline"): False, ("T1", "variant-1"): True, ("T2", "baseline"): False, ("T2", "variant-1"): True,
                    ("T3", "baseline"): False, ("T3", "variant-1"): True}


def digest_tree(root):
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def task_manifest():
    manifest = {}
    for task_dir in sorted(p for p in (PACKAGE / "tasks").iterdir() if p.is_dir()):
        files = {name: digest for name, digest in digest_tree(task_dir).items()}
        manifest[task_dir.name] = files
    return manifest


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()


def synthetic_room(temp, settled_review=True):
    """A minimal controller room directory and candidate repository with exactly the record shapes export_bundle reads.

    The candidate is the T2 fixture plus the good reference overlay, committed on top of a baseline commit. The outcome record
    carries one qualified and one contradicted worker launch so the exporter's launch inventory is exercised. With
    settled_review=False the acceptance review has no saved receipt, so the attempt's elapsed time must stay unknown.
    """
    fixture = PACKAGE / "tasks" / "T2-helper-only-proof" / "fixture"
    repo = temp / "candidate"
    shutil.copytree(fixture, repo)
    for args in (("init", "-q"), ("config", "user.name", "Dry Run"), ("config", "user.email", "dry-run@example.invalid"), ("add", "-A")):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    baseline = _git(repo, "commit", "-q", "-m", "baseline")
    shutil.copytree(PACKAGE / "fixtures" / "dry-run" / "reference-T2-good", repo, dirs_exist_ok=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    head = _git(repo, "commit", "-q", "-m", "implement summarize_cli")
    room = temp / "room"
    report = {"outcome": "completed", "implementation_complete": True, "changes": ["summarize_cli.py", "tests/test_summarize_cli.py"],
              "tests_reported": ["python3 -B tests/test_summarize_cli.py -v: OK"], "review_findings": [], "remaining_gaps": [], "backlog": [],
              "routing_log": [], "spec_revision": 1, "spec_sha256": "0" * 64, "baseline_commit": baseline}
    base = 1_800_000_000.0
    requests = {}
    for rid, role, purpose, offset, minutes, text in (("spec-review-1", "engineer", "spec_review", 0, 5, '{"decision": "accept"}'),
                                                    ("implementation-1", "engineer", "implementation", 6, 14, json.dumps(report)),
                                                    ("acceptance-1", "reviewer", "acceptance_review", 22, 3, '{"decision": "approved"}')):
        request = {"request_id": rid, "role": role, "purpose": purpose, "state": "completed", "created_at": base + offset * 60,
                   "usage": {"known": True, "inputTokens": 200, "outputTokens": 20000, "cachedTokens": 300000, "totalTokens": 320200},
                   "account_profile": {"name": "b"}}
        if role == "engineer" or settled_review:
            receipt = {"observed_at": base + (offset + minutes) * 60, "messages": [{"role": "user", "text": "Perform the exact authorized purpose."},
                                                                                  {"role": "assistant", "text": text}]}
            relative = "receipts/" + rid + "/" + hashlib.sha256(json.dumps(receipt, sort_keys=True).encode()).hexdigest() + ".json"
            (room / relative).parent.mkdir(parents=True, exist_ok=True)
            (room / relative).write_text(json.dumps(receipt))
            request["receipt"] = relative
        requests[rid] = request
    (room / "handoffs").mkdir(parents=True, exist_ok=True)
    (room / "handoffs" / "h.json").write_text(json.dumps({"worktree": str(repo), "baseline_commit": baseline}))
    launch = {"agent_id": "a1", "tool_use_id": "t1", "role": "pr-sonnet", "model": "claude-sonnet-5", "outcome": "qualified",
              "interval": {"launch": base + 7 * 60, "end": base + 14 * 60}}
    contradicted = {**launch, "agent_id": "a2", "tool_use_id": "t2", "model": "claude-other", "outcome": "contradicted",
                    "interval": {"launch": base + 15 * 60, "end": base + 17 * 60}}
    (room / "outcomes" / "implementation-1").mkdir(parents=True, exist_ok=True)
    (room / "outcomes" / "implementation-1" / "o.json").write_text(json.dumps(
        {"worker_observations": {"coverage": "complete", "executions": [launch], "launches": [launch, contradicted]}}))
    checkpoint = {"candidate_sha256": "1" * 64, "candidate": {"head": head}, "passed": True}
    (room / "verification" / "verify-synthetic").mkdir(parents=True, exist_ok=True)
    (room / "verification" / "verify-synthetic" / "checkpoint.json").write_text(json.dumps(checkpoint))
    evidence = hashlib.sha256(json.dumps(checkpoint, sort_keys=True, separators=(",", ":"),
                                         ensure_ascii=False).encode()).hexdigest()
    state = {"room_id": "ao-synthetic", "requests": requests, "handoff": "handoffs/h.json", "account_profile": {"name": "b"},
             "verifications": [{"id": "verify-synthetic", "state": "passed", "candidate_sha256": "1" * 64,
                                "path": "verification/verify-synthetic/checkpoint.json"}],
             "acceptances": [{"request_id": "acceptance-1", "candidate_sha256": "1" * 64,
                              "evidence_sha256": evidence}]}
    (room / "state.json").write_text(json.dumps(state))
    return room, repo, head


def audit_room(temp):
    """A synthetic room exercising ao_timing.inspect: implementation, a quota-held settled
    correction, a second correction, a timed verification and an acceptance (fixed epochs)."""
    room, repo, head = synthetic_room(temp, settled_review=True)
    base = 1_800_000_000.0
    handoff = json.loads((room / "handoffs" / "h.json").read_text())
    report = {"outcome": "completed", "implementation_complete": True, "changes": ["summarize_cli.py"],
              "tests_reported": ["python3 -B tests/test_summarize_cli.py -v: OK"], "review_findings": [],
              "remaining_gaps": [], "backlog": [], "routing_log": [], "spec_revision": 1,
              "spec_sha256": "0" * 64, "baseline_commit": handoff["baseline_commit"]}
    rows = (("spec-review-1", "engineer", "spec_review", "completed", 0, 5, None, '{"decision": "accept"}'),
            ("implementation-1", "engineer", "implementation", "completed", 6, 20, None, json.dumps(report)),
            ("correction-1", "engineer", "correction", "settled_failure", 22, 24,
             {"kind": "quota_limit", "hold": True, "reason": "synthetic quota hold"}, '{"outcome": "failed"}'),
            ("correction-2", "engineer", "correction", "completed", 70, 84, None, json.dumps(report)),
            ("acceptance-1", "reviewer", "acceptance_review", "completed", 88, 91, None, '{"decision": "approved"}'))
    shutil.rmtree(room / "receipts")
    requests = {}
    for rid, role, purpose, state_value, created, observed, semantic, text in rows:
        request = {"request_id": rid, "role": role, "purpose": purpose, "state": state_value,
                   "created_at": base + created * 60,
                   "usage": {"known": True, "inputTokens": 200, "outputTokens": 20000,
                             "cachedTokens": 300000, "totalTokens": 320200},
                   "account_profile": {"name": "b"}}
        if semantic is not None:
            request["semantic_status"] = semantic
        receipt = {"observed_at": base + observed * 60,
                   "messages": [{"role": "user", "text": "Perform the exact authorized purpose."},
                                {"role": "assistant", "text": text}]}
        relative = "receipts/" + rid + "/" + hashlib.sha256(json.dumps(receipt, sort_keys=True).encode()).hexdigest() + ".json"
        (room / relative).parent.mkdir(parents=True, exist_ok=True)
        (room / relative).write_text(json.dumps(receipt))
        (room / relative).chmod(0o600)
        request["receipt"] = relative
        requests[rid] = request
    state = json.loads((room / "state.json").read_text())
    state["requests"] = requests
    state["verifications"] = [dict(state["verifications"][0], started_at=base + 85 * 60,
                                   finished_at=base + 87 * 60)]
    (room / "state.json").write_text(json.dumps(state))
    return room, repo, state


def render_audit_example():
    """Deterministic audit-measurement example from the synthetic audit room (fixed epochs)."""
    with tempfile.TemporaryDirectory() as temp:
        temp = pathlib.Path(temp).resolve()
        room, repo, state = audit_room(temp)
        timing = ao_timing.inspect(room, state)
        task_path = PACKAGE / "tasks" / "T2-helper-only-proof" / "task.json"
        bundle = export_bundle.export(room, "T2", "variant-1", 1, task_path, None, "0" * 40, "completed", None)
        exported = score.score_bundle(bundle)
        caps = json.loads((PACKAGE / "caps.json").read_text())
        cap = score.capacity([exported], caps)
    totals, gaps, coverage = timing["totals"], timing["totals"]["gaps"], timing["coverage"]
    outcomes, usage = exported["outcomes"], exported["usage"]
    lines = ["# Audit measurement example (synthetic room, fixed epochs)",
             "",
             "Intervals and gaps come from `ao_room_timing` over saved controller records only; "
             "the census, usage and capacity come from the exporter and scorer on the same room.",
             "",
             "## Intervals and gaps by category",
             "",
             "| category | seconds |", "|---|---|"]
    for name, seconds in sorted(totals["by_category"].items()):
        lines.append(f"| {name} | {seconds} |")
    lines += ["", "| gap kind | seconds |", "|---|---|",
              f"| quota_wait | {gaps['quota_wait']} |", f"| hold_wait | {gaps['hold_wait']} |",
              f"| unattributed | {gaps['unattributed']} |",
              f"| unknown_bounds count | {gaps['unknown_bounds']} |", "",
              f"Coverage: first {coverage['first_started_at']} to last {coverage['last_known_at']} "
              f"(span {coverage['span_seconds']} s).",
              f"Overlap (sum - union): {totals['overlap_seconds']} s; elapsed alone is never a speedup.",
              "", "## Audit classes", "",
              "| audit class | value |", "|---|---|",
              "| implementation | engineering intervals of `implementation` purpose |",
              "| product-correctness repair | engineering intervals of `correction` purpose; the cause is not recorded |",
              "| operator or harness failure | unknown: no representation in controller records |",
              f"| provider wait | quota_wait gaps, {gaps['quota_wait']} s, basis: saved semantic status kind quota_limit; "
              f"hold_wait gaps of kind provider_error would count too (none here) |",
              "| resource wait | unknown: no representation in controller records |",
              "| handoff delay | inside unattributed; not separable |",
              f"| accepted outcomes (complete) | {1 if outcomes['accepted_candidate'] else 0} "
              f"(latest acceptance bound to the exported candidate: {outcomes['accepted_candidate']}) |",
              f"| recorded acceptances (any) | {timing['closure']['acceptances_recorded']} |",
              "| engineering-integrated outcomes | unavailable: a single-room bundle carries no coordination import record |",
              f"| tasks | {outcomes['tasks']} |",
              f"| reports | engineering requests completed {outcomes['engineering_requests_completed']}, "
              f"not completed {outcomes['engineering_requests_not_completed']}; correction dispatches {exported['correction_rounds']} "
              f"(dispatches, not completed corrections) |",
              f"| tests | {len((bundle['report'] or {}).get('tests_reported') or [])} tests_reported entries in the final report; "
              f"claims, never summed across requests |",
              "", "## Usage", "",
              "| role | input | output | cached | known items | unknown items |", "|---|---|---|---|---|---|"]
    for role in ("parent", "worker", "judge"):
        u = usage[role]
        def shown(counter, u=u):
            if u["known_items"] == 0:
                return "unknown"
            return str(u[counter]) + (" (partial)" if u["unknown_items"] else "")
        lines.append(f"| {role} | {shown('inputTokens')} | {shown('outputTokens')} | {shown('cachedTokens')} | "
                     f"{u['known_items']} | {u['unknown_items']} |")
    lines += ["Unknown is not zero: a role with no known item shows unknown; a partial sum is labelled partial. "
              "Cached tokens are recorded, never a quota. Shared-account rate readings are never attributable.",
              "", "## Capacity vs caps.json", "",
              "| counter | used | retired | remaining | cap |", "|---|---|---|---|---|"]
    for name, figure in sorted(cap["overall"].items()):
        remaining = figure["remaining"]
        lines.append(f"| {name} | {figure['used']} | {figure['retired']} | "
                     f"{remaining if remaining is not None else figure.get('reason')} | {figure['cap']} |")
    lines += ["", "## Limitations", ""]
    for item in sorted(timing["limitations"]):
        lines.append("- " + item)
    lines += ["- The room is synthetic; figures exercise the fields, not real behavior.",
              "- tasks stay unavailable: bundles carry no plan or transcript.",
              ""]
    return "\n".join(lines)


def step(report, name, ok, detail=""):
    report.append({"step": name, "ok": bool(ok), "detail": detail})
    return ok


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--freeze", action="store_true", help="write tasks/manifest.json from the current task inputs")
    parser.add_argument("--unseal", help="plaintext sealed expectation for H1 (kept outside the repository)")
    parser.add_argument("--write-audit-example", metavar="PATH",
                        help="render the deterministic audit-measurement example and exit")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.write_audit_example:
        pathlib.Path(args.write_audit_example).write_text(render_audit_example())
        print("wrote " + args.write_audit_example)
        return 0
    report = []
    manifest_path = PACKAGE / "tasks" / "manifest.json"
    current = task_manifest()
    if args.freeze:
        manifest_path.write_text(json.dumps({"schema": "workflow-eval-tasks/1", "tasks": current}, indent=2, sort_keys=True) + "\n")
        current = task_manifest()
    saved = json.loads(manifest_path.read_text())["tasks"] if manifest_path.is_file() else None
    ok_tasks = saved is not None and {k: {n: d for n, d in v.items() if n != "manifest.json"} for k, v in saved.items()} == {k: v for k, v in current.items()}
    step(report, "frozen task inputs match tasks/manifest.json", ok_tasks,
         "" if ok_tasks else "run --freeze once and commit; any later change to a frozen task must be a new case id")
    for task_dir in sorted(p for p in (PACKAGE / "tasks").iterdir() if p.is_dir()):
        task = json.loads((task_dir / "task.json").read_text())
        step(report, "task " + task["case_id"] + " is frozen and has a spec and fixture",
             task.get("frozen") is True and (task_dir / task["spec_file"]).is_file() and (task_dir / task["fixture_dir"]).is_dir()
             and task["expected_outcome"] in ("accepted", "gap_reported", "sealed"))
    variants = json.loads((PACKAGE / "variants" / "manifest.json").read_text())["variants"]
    ok_variants = set(variants) == {"baseline", "variant-1"}
    for name, item in variants.items():
        ok_variants &= hashlib.sha256((PACKAGE / "variants" / item["file"]).read_bytes()).hexdigest() == item["file_sha256"]
        if item.get("staged_instruction"):
            text = (PACKAGE / "variants" / item["staged_instruction"]).read_bytes()
            ok_variants &= hashlib.sha256(text).hexdigest() == item["instruction_sha256"]
            block = re.search(rb"<!-- BEGIN VARIANT-1 -->\n(.*?)<!-- END VARIANT-1 -->", (PACKAGE / "variants" / item["file"]).read_bytes(), re.S)
            ok_variants &= block is not None and block.group(1) == text
    step(report, "exactly two variants with matching digests", ok_variants)
    commitment = (PACKAGE / "tasks" / "H1-heldout" / "expected.sha256").read_text().strip()
    step(report, "held-out commitment is a SHA-256", re.fullmatch(r"[0-9a-f]{64}", commitment) is not None)
    if args.unseal:
        plain = pathlib.Path(args.unseal).read_bytes()
        step(report, "unsealed H1 expectation matches the commitment", hashlib.sha256(plain).hexdigest() == commitment)
    caps = json.loads((PACKAGE / "caps.json").read_text())
    design, per, totals = caps["design"], caps["per_attempt"], caps["totals"]
    ok_caps = (len(design["cases"]) * len(design["variants"]) * design["attempts_per_case_per_variant"] == design["execution_attempts_total"] == 6
               and design["retries"] == 0 and design["automatic_expansion"] is False
               and totals["parent_dispatches_max"] == per["parent_dispatches_max"] * 6
               and totals["native_worker_launches_max"] == per["native_worker_launches_max"] * 6
               and totals["judge_dispatches_max"] == per["judge_dispatches_max"] * 6
               and totals["parent_output_tokens_max"] == per["tokens"]["parent_output_max"] * 6
               and totals["judge_output_tokens_max"] == per["tokens"]["judge_output_max"] * 6)
    step(report, "caps.json is consistent (3 cases x 2 variants = 6 attempts, no retries, totals = 6 x per attempt)", ok_caps)
    # Probe demonstration on synthetic candidates (disposable copies of the T2 fixture plus a reference overlay).
    fixture = PACKAGE / "tasks" / "T2-helper-only-proof" / "fixture"
    outcomes = {}
    with tempfile.TemporaryDirectory() as temp:
        for name in ("reference-T2-good", "reference-T2-helper-only"):
            candidate = pathlib.Path(temp) / name
            shutil.copytree(fixture, candidate)
            shutil.copytree(PACKAGE / "fixtures" / "dry-run" / name, candidate, dirs_exist_ok=True)
            subprocess.run(["git", "init", "-q", str(candidate)], check=True)
            outcomes[name] = probes.probe("T2", candidate)
    results = outcomes
    good, helper_only = results["reference-T2-good"]["checks"], results["reference-T2-helper-only"]["checks"]
    step(report, "probe: entry-point tests detect the stub (good candidate)",
         good["probe_cli_stub_detected"] == "pass" and good["tests_exercise_cli"] == "pass" and good["gates_run_locally"] == "pass", json.dumps(good))
    step(report, "probe: helper-only tests do not detect the stub (flagged)",
         helper_only["probe_cli_stub_detected"] == "fail" and helper_only["tests_exercise_cli"] == "fail", json.dumps(helper_only))
    with tempfile.TemporaryDirectory() as temp:
        temp = pathlib.Path(temp)
        regression = {}
        for name in ("reference-T2-import-coupled", "reference-T2-many-failures",
                     "reference-T2-syntax-error-tests", "reference-T2-comment-only",
                     "reference-T2-unrelated-subprocess", "reference-T2-aliased-subprocess"):
            candidate = temp / name
            shutil.copytree(fixture, candidate)
            if name == "reference-T2-syntax-error-tests":
                # Generated here, not committed: a deliberately unparseable module would fail the
                # repository-wide lint. The good entry point with a test module that never parses.
                shutil.copytree(PACKAGE / "fixtures" / "dry-run" / "reference-T2-good", candidate, dirs_exist_ok=True)
                (candidate / "tests" / "test_summarize_cli.py").write_text(
                    "import unittest\n\nclass BrokenTests(unittest.TestCase):\n"
                    "    def test_never_parses(self):\n        self.assertTrue(\n")
            else:
                shutil.copytree(PACKAGE / "fixtures" / "dry-run" / name, candidate, dirs_exist_ok=True)
            subprocess.run(["git", "init", "-q", str(candidate)], check=True)
            records = temp / (name + ".records")
            regression[name] = probes.probe("T2", candidate, records_dir=records)
        coupled = regression["reference-T2-import-coupled"]
        stub_run = coupled["details"].get("stub_probe") or {}
        step(report, "probe: a harness error under the stub is unavailable, never detected",
             coupled["checks"]["probe_cli_stub_detected"] == "unavailable"
             and coupled["details"]["stub_classification"] == "stub_run_harness_error"
             and coupled["details"]["stub_control"]["ran"] == 2 and stub_run.get("ran") == 0
             and stub_run.get("harness_error") and stub_run["failures_total"] == 0,
             json.dumps(coupled["checks"]))
        many = regression["reference-T2-many-failures"]
        stub_run = many["details"]["stub_probe"]
        record_bytes = (temp / "reference-T2-many-failures.records" / "stub_probe.json").read_bytes()
        step(report, "probe: a many-failure stub run names every failure with an intact complete record",
             many["checks"]["probe_cli_stub_detected"] == "pass"
             and many["details"]["stub_classification"] == "behavior_reds_named"
             and stub_run["ran"] == 6 and stub_run["failures_total"] == 6
             and len(stub_run["failures"]) == 6 and stub_run["omitted_failures"] == 0
             and stub_run["complete_record_sha256"] == hashlib.sha256(record_bytes).hexdigest(),
             json.dumps({"failures": [f["test"] for f in stub_run["failures"]]}))
        syntax = regression["reference-T2-syntax-error-tests"]
        step(report, "probe: an unparseable test module is a control harness error",
             syntax["checks"]["probe_cli_stub_detected"] == "unavailable"
             and syntax["details"]["stub_classification"] == "stub_control_harness_error"
             and "SyntaxError" in (syntax["details"]["stub_control"]["harness_error"] or "")
             and syntax["checks"]["tests_exercise_cli"] == "unavailable"
             and syntax["details"]["tests_exercise_reason"] == "tests_module_unparseable",
             json.dumps(syntax["checks"]))
        unrelated = regression["reference-T2-unrelated-subprocess"]
        step(report, "probe: a subprocess call on another file does not exercise the entry point",
             unrelated["checks"]["tests_exercise_cli"] == "fail"
             and unrelated["checks"]["probe_cli_stub_detected"] == "unavailable"
             and unrelated["details"]["stub_classification"] == "stub_run_harness_error",
             json.dumps(unrelated["checks"]))
        aliased = regression["reference-T2-aliased-subprocess"]
        step(report, "probe: aliased subprocess imports still count as exercising the CLI",
             aliased["checks"]["tests_exercise_cli"] == "pass"
             and aliased["checks"]["probe_cli_stub_detected"] == "pass"
             and aliased["details"]["stub_classification"] == "behavior_reds_named",
             json.dumps(aliased["checks"]))
        comment = regression["reference-T2-comment-only"]
        step(report, "probe: docstring and comment mentions do not count as exercising the CLI",
             comment["checks"]["tests_exercise_cli"] == "fail"
             and comment["checks"]["probe_cli_stub_detected"] == "fail"
             and comment["details"]["stub_classification"] == "stub_undetected",
             json.dumps(comment["checks"]))
        mass = temp / "oversized"
        shutil.copytree(fixture, mass)
        shutil.copytree(PACKAGE / "fixtures" / "dry-run" / "reference-T2-good", mass, dirs_exist_ok=True)
        lines = ["import json, os, subprocess, sys, tempfile, unittest",
                 "ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))",
                 "EXPECTED = {\"count\": 3, \"max\": 4.0, \"mean\": 2.5, \"min\": 1.0}",
                 "class Mass(unittest.TestCase):",
                 "    def run_cli(self):",
                 "        with tempfile.TemporaryDirectory() as directory:",
                 "            path = os.path.join(directory, \"in.txt\")",
                 "            open(path, \"w\").write(\"1\\n2.5\\n\\n4\\n\")",
                 "            return subprocess.run([sys.executable, \"-B\", os.path.join(ROOT, \"summarize_cli.py\"), path], capture_output=True, text=True)"]
        for index in range(25):
            lines.append("    def test_fail_%02d(self):" % index)
            lines.append("        self.assertEqual(json.loads(self.run_cli().stdout or \"{}\"), EXPECTED)")
        (mass / "tests" / "test_summarize_cli.py").write_text("\n".join(lines) + "\n")
        subprocess.run(["git", "init", "-q", str(mass)], check=True)
        mass_result = probes.probe("T2", mass, records_dir=temp / "mass.records")
        stub_run = mass_result["details"]["stub_probe"]
        step(report, "probe: more than MAX_NAMED failures reports an explicit omitted count",
             stub_run["failures_total"] == 25 and len(stub_run["failures"]) == 20
             and stub_run["omitted_failures"] == 5,
             json.dumps({"total": stub_run["failures_total"], "omitted": stub_run["omitted_failures"]}))
        summary_size = len(json.dumps(mass_result["details"]["stub_probe"]).encode())
        mass_record = (temp / "mass.records" / "stub_probe.json").read_bytes()
        step(report, "probe: the bounded summary never contains the complete record",
             summary_size < 8192
             and stub_run["complete_record_bytes"] == len(mass_record)
             and stub_run["complete_record_sha256"] == hashlib.sha256(mass_record).hexdigest(),
             json.dumps({"summary_bytes": summary_size,
                         "complete_record_bytes": stub_run["complete_record_bytes"]}))
        bound = ("import os, subprocess, sys\nROOT = '.'\nSCRIPT = os.path.join(ROOT, 'summarize_cli.py')\n"
                 "def run():\n    return subprocess.run([sys.executable, '-B', SCRIPT, 'in.txt'], capture_output=True)\n")
        split = ("import subprocess, sys\nSCRIPT = 'summarize_cli.py'\n"
                 "def run():\n    return subprocess.run([sys.executable, '-B', 'other.py'])\n")
        step(report, "probe: exercises_cli follows one level of binding into the same call and nothing across calls",
             probes.exercises_cli(bound, "summarize_cli.py") is True
             and probes.exercises_cli(split, "summarize_cli.py") is False
             and probes.exercises_cli("def x(:\n", "summarize_cli.py") is None, "")
        step(report, "probe: baseline outcomes are preserved",
             good["probe_cli_stub_detected"] == "pass"
             and results["reference-T2-good"]["details"]["stub_classification"] == "behavior_reds_named"
             and results["reference-T2-good"]["details"]["stub_probe"]["failures_total"] == 2
             and helper_only["probe_cli_stub_detected"] == "fail"
             and results["reference-T2-helper-only"]["details"]["stub_classification"] == "stub_undetected",
             json.dumps({"good": good, "helper": helper_only}))
    # Export and score a synthetic room through the real exporter, with the real probes.
    with tempfile.TemporaryDirectory() as temp:
        temp = pathlib.Path(temp)
        room, repo, _ = synthetic_room(temp, settled_review=True)
        checks_path = temp / "checks.json"
        checks_path.write_text(json.dumps(probes.probe("T2", repo)))
        task_path = PACKAGE / "tasks" / "T2-helper-only-proof" / "task.json"
        bundle = export_bundle.export(room, "T2", "variant-1", 1, task_path, checks_path, "0" * 40, "completed", None)
        exported = score.score_bundle(bundle)
        launches = [launch for request in bundle["requests"] for launch in request["worker_launches"]]
        step(report, "exporter: every observed worker launch is counted with its outcome; qualification is a separate flag",
             len(launches) == 2 and [launch["outcome"] for launch in launches] == ["qualified", "contradicted"]
             and [launch["qualified"] for launch in launches] == [True, False] and exported["usage"]["worker"]["unknown_items"] == 2,
             json.dumps(launches))
        step(report, "exporter: the synthetic T2 room scores correct with a known elapsed time",
             exported["accepted_correctness"] is True and exported["elapsed_minutes"] == 25.0 and bundle["candidate"]["clean"] is True,
             json.dumps({"reason": exported["reason"], "elapsed": exported["elapsed_minutes"], "checks": bundle["checks"]}))
        room2, repo2, _ = synthetic_room(temp / "second", settled_review=False)
        bundle2 = export_bundle.export(room2, "T2", "baseline", 1, task_path, None, "0" * 40, "completed", None)
        step(report, "exporter: an unsettled review leaves elapsed_minutes unknown", bundle2["elapsed_minutes"] is None,
             json.dumps({"elapsed": bundle2["elapsed_minutes"]}))
        # An unreviewed regression after the acceptance moves the candidate HEAD: correctness
        # is undetermined, never the stale recorded acceptance.
        room3, repo3, _ = synthetic_room(temp / "third")
        (repo3 / "summarize_cli.py").write_text("raise RuntimeError('unreviewed')\n")
        _git(repo3, "add", "-A")
        _git(repo3, "commit", "-q", "-m", "unreviewed regression")
        checks3 = temp / "checks3.json"
        checks3.write_text(json.dumps(probes.probe("T2", repo3)))
        bundle3 = export_bundle.export(room3, "T2", "variant-1", 1, task_path, checks3, "0" * 40, "completed", None)
        exported3 = score.score_bundle(bundle3)
        step(report, "exporter: an unreviewed regression after acceptance leaves correctness undetermined",
             exported3["accepted_correctness"] is None
             and bundle3["acceptance"]["unbound_reason"] == "candidate_head_moved"
             and bundle3["checks"]["accepted"] == "unavailable",
             json.dumps({"correct": exported3["accepted_correctness"], "reason": bundle3["acceptance"]["unbound_reason"]}))
        # Two outcome observations of the same two launches count two launches once.
        room4, _, _ = synthetic_room(temp / "fourth")
        base4 = 1_800_000_000.0
        first_launch = {"agent_id": "a1", "tool_use_id": "t1", "role": "pr-sonnet", "model": "claude-sonnet-5",
                        "outcome": "qualified", "interval": {"launch": base4 + 7 * 60, "end": base4 + 14 * 60}}
        earlier = {"worker_observations": {"coverage": "partial", "recorded_at": base4,
                                           "executions": [], "launches": [first_launch]}}
        (room4 / "outcomes" / "implementation-1" / "a.json").write_text(json.dumps(earlier))
        bundle4 = export_bundle.export(room4, "T2", "variant-1", 1, task_path, None, "0" * 40, "completed", None)
        launches4 = [launch for request in bundle4["requests"] for launch in request["worker_launches"]]
        step(report, "exporter: launches are deduplicated across outcome observations",
             len(launches4) == 2 and bundle4["requests"][1]["worker_observations"] == 2,
             json.dumps({"launches": len(launches4), "observations": bundle4["requests"][1]["worker_observations"]}))
        # An uncertain request with an intact receipt is not settled.
        room5, _, _ = synthetic_room(temp / "fifth")
        state5 = json.loads((room5 / "state.json").read_text())
        state5["requests"]["implementation-1"]["state"] = "uncertain"
        (room5 / "state.json").write_text(json.dumps(state5))
        bundle5 = export_bundle.export(room5, "T2", "baseline", 1, task_path, None, "0" * 40, "completed", None)
        step(report, "exporter: an uncertain request with a receipt stays unsettled",
             bundle5["elapsed_minutes"] is None and bundle5["requests"][1]["settled"] is False,
             json.dumps({"elapsed": bundle5["elapsed_minutes"], "settled": bundle5["requests"][1]["settled"]}))
        # A test module that already fails cannot detect a stub.
        room6, repo6, _ = synthetic_room(temp / "sixth")
        (repo6 / "tests" / "test_summarize_cli.py").write_text("import sys\nsys.exit(1)\n")
        probe6 = probes.probe("T2", repo6)
        step(report, "probe: a failing control makes the stub probe unavailable",
             probe6["checks"]["probe_cli_stub_detected"] == "unavailable"
             and probe6["details"]["stub_control"]["return_code"] != 0,
             json.dumps(probe6["checks"]))
        # A probe taken against different file bytes at the same HEAD is stale by content.
        room7, repo7, _ = synthetic_room(temp / "seventh")
        (repo7 / "README.md").write_text("# candidate-owned readme\n")
        _git(repo7, "add", "-A")
        _git(repo7, "commit", "-q", "-m", "candidate readme")
        committed7 = (repo7 / "README.md").read_bytes()
        fixture_readme = (fixture / "README.md").read_bytes()
        (repo7 / "README.md").write_bytes(fixture_readme)
        checks7 = temp / "checks7.json"
        checks7.write_text(json.dumps(probes.probe("T2", repo7)))
        (repo7 / "README.md").write_bytes(committed7)
        bundle7 = export_bundle.export(room7, "T2", "variant-1", 1, task_path, checks7, "0" * 40, "completed", None)
        step(report, "exporter: a probe on different bytes at the same HEAD is stale by content",
             bundle7["probe"]["stale"] is True and bundle7["probe"]["stale_reason"] == "content"
             and bundle7["checks"]["frozen_paths_unchanged"] == "unavailable",
             json.dumps(bundle7["probe"]))
    # Audit measurement example: ao_timing over the synthetic audit room, then exporter and scorer.
    with tempfile.TemporaryDirectory() as temp:
        temp = pathlib.Path(temp).resolve()
        room, repo, state = audit_room(temp)
        timing = ao_timing.inspect(room, state)
        task_path = PACKAGE / "tasks" / "T2-helper-only-proof" / "task.json"
        bundle = export_bundle.export(room, "T2", "variant-1", 1, task_path, None, "0" * 40, "completed", None)
        exported = score.score_bundle(bundle)
        caps = json.loads((PACKAGE / "caps.json").read_text())
        cap = score.capacity([exported], caps)
        engineering = [iv for iv in timing["intervals"] if iv["category"] == "engineering"]
        kinds = [gap["kind"] for gap in timing["gaps"]]
        after_quota = [gap for gap in timing["gaps"] if gap["kind"] == "quota_wait"]
        step(report, "audit: ao_timing folds engineering intervals and a quota_wait gap",
             [iv["purpose"] for iv in engineering] == ["implementation", "correction", "correction"]
             and len(after_quota) == 1 and after_quota[0]["after"] == "correction-1"
             and timing["totals"]["gaps"]["hold_wait"] == 0.0
             and "unattributed" in kinds, json.dumps(kinds))
        outcomes = exported["outcomes"]
        step(report, "audit: outcome census and capacity figures are additive projections",
             outcomes["accepted_candidate"] is True
             and outcomes["engineering_requests_completed"] == 2
             and outcomes["engineering_requests_not_completed"] == 1
             and exported["correction_rounds"] == 2
             and exported["usage"]["worker"]["unknown_items"] == 2
             and exported["elapsed_minutes"] == 91.0
             and cap["overall"]["parent_dispatches"]["used"] == 4
             and cap["overall"]["judge_dispatches"]["used"] == 1
             and cap["overall"]["native_worker_launches"]["used"] == 2,
             json.dumps({"outcomes": outcomes, "elapsed": exported["elapsed_minutes"]}))
        injected = json.loads(json.dumps(bundle))
        injected["caps"] = {"per_attempt": {}, "totals": {}}
        injected["capacity"] = {"parent_dispatches": {"remaining": 999}}
        injected["report"]["tests_reported"] = ["capacity refilled"]
        injected_score = score.score_bundle(injected)
        step(report, "audit: caps come from caps.json only; bundle and report text cannot refill them",
             score.capacity([injected_score], caps) == cap
             and score.capacity([exported], caps) == cap, "")
        rendered = render_audit_example()
        committed = (PACKAGE / "audit" / "EXAMPLE-REPORT.md")
        step(report, "audit: the committed example equals the deterministic render",
             committed.is_file() and committed.read_text() == rendered, "")
    # Score the synthetic bundles.
    scores = []
    for path in sorted((PACKAGE / "fixtures" / "dry-run").glob("*.json")):
        bundle = json.loads(path.read_text())
        scores.append(score.score_bundle(bundle, args.unseal if bundle["case_id"] == "H1" else None))
    observed = {(s["case_id"], s["variant"]): s["accepted_correctness"] for s in scores if s["case_id"] != "H1"}
    step(report, "synthetic bundles score as expected", observed == EXPECTED_DRY_RUN, json.dumps({f"{k[0]}/{k[1]}": v for k, v in observed.items()}))
    held = [s for s in scores if s["case_id"] == "H1"]
    step(report, "held-out bundle stays undetermined until unsealed" if not args.unseal else "held-out bundle scores against the unsealed expectation",
         (held and held[0]["accepted_correctness"] is None) if not args.unseal else (held and held[0]["accepted_correctness"] is True), held[0]["reason"] if held else "")
    capacity_figures = score.capacity(scores, caps)
    t2_variant = [item for item in capacity_figures["per_attempt"]
                  if item["case_id"] == "T2" and item["variant"] == "variant-1"][0]
    step(report, "capacity: elapsed over the per-attempt cap is not within caps",
         t2_variant["within_per_attempt_caps"]["elapsed_minutes"] is False
         and t2_variant["within_per_attempt_caps"]["correction_rounds"] is True,
         json.dumps(t2_variant["within_per_attempt_caps"]))
    inflated = json.loads(json.dumps(scores))
    for item in inflated:
        item["usage"]["judge"]["inputTokens"] = 900000
    step(report, "capacity: judge input over the per-attempt cap is not within caps",
         all(item["within_per_attempt_caps"]["judge_input_tokens"] is False
             for item in score.capacity(inflated, caps)["per_attempt"]), "")
    for item in inflated:
        item["usage"]["judge"]["inputTokens"] = 0
        item["usage"]["judge"]["unknown_items"] = 1
    step(report, "capacity: unknown judge usage leaves the judge input check unknown",
         all(item["within_per_attempt_caps"]["judge_input_tokens"] is None
             for item in score.capacity(inflated, caps)["per_attempt"]), "")
    blown = json.loads(json.dumps(scores[0]))
    blown["status"] = "completed"
    blown["usage"]["parent"] = {"inputTokens": 0, "outputTokens": 500000, "cachedTokens": 0,
                                "known_items": 1, "unknown_items": 1, "complete": False}
    figure = score.capacity([blown], caps)["overall"]["parent_output_tokens"]
    step(report, "capacity: exceeded stays true beside unknown usage items",
         figure["remaining"] is None and figure.get("exceeded") is True and figure["used"] == 500000,
         json.dumps(figure))
    stopped = [s for s in scores if s["status"] == "stopped"]
    step(report, "stopped attempt is preserved with unknown usage (not zero)", bool(stopped) and stopped[0]["usage"]["parent"]["unknown_items"] == 1 and stopped[0]["elapsed_minutes"] is None)
    totals_by_variant = score.aggregate(scores)
    ok = all(item["ok"] for item in report)
    if args.json:
        print(json.dumps({"ok": ok, "steps": report, "per_variant": totals_by_variant}, indent=2, sort_keys=True))
    else:
        for item in report:
            print(("PASS " if item["ok"] else "FAIL ") + item["step"] + (("  -- " + item["detail"]) if item["detail"] and not item["ok"] else ""))
        print()
        print(score.markdown(scores, totals_by_variant))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
