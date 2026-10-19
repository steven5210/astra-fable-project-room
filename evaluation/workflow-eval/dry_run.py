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
import score  # noqa: E402
import probes  # noqa: E402

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


def step(report, name, ok, detail=""):
    report.append({"step": name, "ok": bool(ok), "detail": detail})
    return ok


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--freeze", action="store_true", help="write tasks/manifest.json from the current task inputs")
    parser.add_argument("--unseal", help="plaintext sealed expectation for H1 (kept outside the repository)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
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
            outcomes[name] = probes.probe("T2", candidate)["checks"]
    good, helper_only = outcomes["reference-T2-good"], outcomes["reference-T2-helper-only"]
    step(report, "probe: entry-point tests detect the stub (good candidate)",
         good["probe_cli_stub_detected"] == "pass" and good["tests_exercise_cli"] == "pass" and good["gates_run_locally"] == "pass", json.dumps(good))
    step(report, "probe: helper-only tests do not detect the stub (flagged)",
         helper_only["probe_cli_stub_detected"] == "fail" and helper_only["tests_exercise_cli"] == "fail", json.dumps(helper_only))
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
