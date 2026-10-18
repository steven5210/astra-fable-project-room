"""Merge CI timing artifacts into a Markdown step summary and a combined JSON
record. Diagnostic only: the script exits 0 even when inputs are missing or
malformed, each section degrades to a "not available" note on error, and it
must never hide the unittest results, which remain the gates.

    python3 tests/ci_timing_summary.py --records DIR --jobs JOBS.json \
        --combined OUT.json --markdown SUMMARY.md

Inputs are untrusted and bounded: records are walked recursively under
--records, skipped when over 8 MiB, non-JSON, carrying the wrong format marker
or fields of the wrong type, and are never used as paths, commands or format
strings. The jobs file is the GitHub Actions jobs API shape; `gh api
--paginate` concatenates page objects, so the file is decoded with
JSONDecoder.raw_decode. Job queue/setup/exec definitions mirror the manual
measurements: queue = job started_at - created_at; the test step is the first
step whose name starts with "Run " and contains "tests" and whose conclusion
is not "skipped"; setup = that step's started_at - job started_at; exec = the
step's duration; total = job completed_at - started_at.
"""
import argparse
import importlib.util
import json
import math
import os
import re
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

RECORD_FORMAT = "project_room_test_timing_v1"
COMBINED_FORMAT = "project_room_timing_combined_v1"
MAX_RECORD_BYTES = 8 * 1024 * 1024
JOB_NAME_RE = re.compile(r"^unittest \((?P<os>[^,]+), (?P<py>[^,]+), (?P<shard>\d+)\)$")
OS_LABELS = {"ubuntu-latest": "linux", "macos-latest": "macos"}
FRACTION_CLIP = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6})\d+(.*)$")

_shard_spec = importlib.util.spec_from_file_location(
    "run_test_shards", Path(__file__).resolve().parent / "run_test_shards.py")
shardtools = importlib.util.module_from_spec(_shard_spec)
_shard_spec.loader.exec_module(shardtools)


def _num(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _iso(value):
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    clipped = FRACTION_CLIP.match(text)
    if clipped:
        text = clipped.group(1) + clipped.group(2)
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _minutes(seconds):
    return "n/a" if seconds is None else f"{seconds / 60:.1f}m"


def _seconds(seconds):
    return "n/a" if seconds is None else f"{seconds:.0f}s"


def _cell(text):
    return re.sub(r"[|\r\n\t]+", " ", str(text))[:120]


def _clean_module_stats(stats):
    if not isinstance(stats, dict):
        return None
    clean = {}
    for field in ("tests", "wall_seconds", "process_seconds", "test_seconds",
                  "setup_seconds", "body_seconds", "teardown_seconds",
                  "fixture_gap_seconds"):
        clean[field] = _num(stats.get(field))
    outcomes = stats.get("outcomes")
    if isinstance(outcomes, dict):
        clean["outcomes"] = {str(key): int(value) for key, value in outcomes.items()
                             if _num(value) is not None}
    return clean


def _clean_summary(summary):
    if not isinstance(summary, dict):
        return {}
    clean = {}
    for field in ("ran", "failures", "errors", "skipped", "expected_failures",
                  "unexpected_successes"):
        value = _num(summary.get(field))
        clean[field] = int(value) if value is not None else 0
    clean["was_successful"] = summary.get("was_successful") is True
    return clean


def _check_record(data, name):
    """Return a bounded copy of one record, or None when it is unusable."""
    if not isinstance(data, dict) or data.get("format") != RECORD_FORMAT:
        return None
    modules, summary, shard = data.get("modules"), data.get("summary"), data.get("shard")
    os_name, python_minor = data.get("os"), data.get("python_minor")
    if not isinstance(modules, dict) or not isinstance(summary, dict):
        return None
    if shard is not None and not isinstance(shard, dict):
        return None
    if not isinstance(os_name, str) or not isinstance(python_minor, str):
        return None
    clean_modules = {}
    for module, stats in modules.items():
        if not isinstance(module, str):
            return None
        clean = _clean_module_stats(stats)
        if clean is None:
            return None
        clean_modules[module[:300]] = clean
    shard_clean = None
    if isinstance(shard, dict):
        index, count = _num(shard.get("index")), _num(shard.get("count"))
        shard_clean = {"index": int(index) if index is not None else None,
                       "count": int(count) if count is not None else None}
    revision, run_id = data.get("revision"), data.get("run_id")
    return {"file": name, "os": os_name, "python": python_minor,
            "shard_index": shard_clean["index"] if shard_clean else None,
            "shard": shard_clean,
            "complete": data.get("complete") is True,
            "summary": _clean_summary(summary),
            "modules": clean_modules,
            "truncated": data.get("tests_truncated") is True,
            "revision": revision if isinstance(revision, str) else None,
            "run_id": run_id if isinstance(run_id, str) else None}


def load_records(root):
    records, ignored = [], []
    root = Path(root)
    if not root.is_dir():
        ignored.append({"file": str(root), "reason": "records directory missing"})
        return records, ignored
    # A `workers/` subtree holds the per-module worker records of a local
    # --record run; the merged shard record is the only one that counts.
    for path in sorted(p for p in root.rglob("*.json")
                       if "workers" not in p.relative_to(root).parts):
        name = str(path.relative_to(root))
        try:
            if path.stat().st_size > MAX_RECORD_BYTES:
                ignored.append({"file": name, "reason": "over 8 MiB"})
                continue
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            ignored.append({"file": name, "reason": "unreadable json"})
            continue
        record = _check_record(data, name)
        if record is None:
            ignored.append({"file": name, "reason": "wrong format or field types"})
            continue
        records.append(record)
    return records, ignored


def load_jobs(path):
    text = Path(path).read_text()
    decoder, index, jobs = json.JSONDecoder(), 0, []
    while index < len(text):
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            break
        obj, index = decoder.raw_decode(text, index)
        if isinstance(obj, dict):
            jobs.extend(job for job in obj.get("jobs", []) if isinstance(job, dict))
    return jobs


def job_metrics(job):
    name = job.get("name")
    started, created = _iso(job.get("started_at")), _iso(job.get("created_at"))
    completed = _iso(job.get("completed_at"))
    metrics = {"name": name if isinstance(name, str) else "?",
               "queue": (started - created).total_seconds() if started and created else None,
               "setup": None, "exec": None,
               "total": (completed - started).total_seconds() if completed and started else None,
               "conclusion": job.get("conclusion") or job.get("status") or "?",
               "os": None, "python": None, "shard": None}
    match = JOB_NAME_RE.match(metrics["name"])
    if match:
        label = match.group("os")
        metrics["os"] = OS_LABELS.get(label, label)
        metrics["python"] = match.group("py")
        metrics["shard"] = int(match.group("shard"))
    steps = job.get("steps")
    for step in steps if isinstance(steps, list) else []:
        if not isinstance(step, dict):
            continue
        step_name = step.get("name")
        if not isinstance(step_name, str):
            continue
        if not step_name.startswith("Run ") or "tests" not in step_name:
            continue
        if step.get("conclusion") == "skipped":
            continue
        step_started, step_completed = _iso(step.get("started_at")), _iso(step.get("completed_at"))
        if step_started and started:
            metrics["setup"] = (step_started - started).total_seconds()
        if step_started and step_completed:
            metrics["exec"] = (step_completed - step_started).total_seconds()
        break
    return metrics


def _record_for(records_by_key, metrics):
    if metrics["os"] is None:
        return None
    return records_by_key.get((metrics["os"], metrics["python"], metrics["shard"]))


def _provenance(records):
    revisions = sorted({r["revision"] for r in records if r["revision"]})
    run_ids = sorted({r["run_id"] for r in records if r["run_id"]})
    revision = (revisions[0] if revisions
                else os.environ.get("PROJECT_ROOM_TIMING_REVISION"))
    run_id = run_ids[0] if run_ids else os.environ.get("GITHUB_RUN_ID")
    return revision, run_id, len(revisions) > 1


def section_header(records, ignored, jobs):
    revision, run_id, mixed = _provenance(records)
    complete = sum(1 for r in records if r["complete"])
    lines = ["## Test timing summary", "",
             f"- revision: `{revision or 'n/a'}`"
             + (" (mixed revisions present)" if mixed else ""),
             f"- run id: `{run_id or 'n/a'}`,"
             f" event: `{os.environ.get('GITHUB_EVENT_NAME') or 'n/a'}`",
             f"- jobs: {len(jobs)}; records: {len(records)} found"
             f" ({complete} complete, {len(records) - complete} incomplete);"
             f" ignored files: {len(ignored)}"]
    for entry in ignored:
        lines.append(f"  - ignored `{_cell(entry['file'])}`: {_cell(entry['reason'])}")
    return "\n".join(lines)


def section_jobs(jobs_metrics, records_by_key):
    lines = ["", "### Jobs", "",
             "| job | os | py | shard | queue | setup | exec | total |"
             " conclusion | tests | record |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for metrics in jobs_metrics:
        record = _record_for(records_by_key, metrics)
        if metrics["os"] is None:
            state, ran = "-", "-"
        elif record is None:
            state, ran = "missing", "n/a"
        else:
            state = "complete" if record["complete"] else "incomplete"
            ran = str(record["summary"].get("ran", 0))
        lines.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} |"
                     .format(_cell(metrics["name"]), metrics["os"] or "-",
                             metrics["python"] or "-",
                             metrics["shard"] if metrics["shard"] is not None else "-",
                             _minutes(metrics["queue"]), _minutes(metrics["setup"]),
                             _minutes(metrics["exec"]), _minutes(metrics["total"]),
                             _cell(metrics["conclusion"]), ran, state))
    return "\n".join(lines)


def section_imbalance(jobs_metrics, baselines=None):
    groups = {}
    for metrics in jobs_metrics:
        if metrics["os"] is None or metrics["exec"] is None:
            continue
        groups.setdefault((metrics["os"], metrics["python"]), []).append(metrics)
    lines = ["", "### Shard imbalance", ""]
    runner_minutes = sum(m["total"] for m in jobs_metrics
                         if m["total"] is not None
                         and m["name"] != "timing-summary")
    queues = [m["queue"] for m in jobs_metrics if m["queue"] is not None]
    for key in sorted(groups):
        execs = sorted(m["exec"] for m in groups[key])
        median = statistics.median(execs)
        ratio = execs[-1] / median if median else float("inf")
        line = (f"- {key[0]}/{key[1]}: exec max {_minutes(execs[-1])},"
                f" median {_minutes(median)}, max/median {ratio:.2f},"
                f" sum {_minutes(sum(execs))}")
        baseline = baselines["execs"].get(key) if baselines else None
        if baseline:
            maxes, medians = baseline["max"], baseline["median"]
            line += (f"; baseline max {_minutes(statistics.median(maxes))},"
                     f" median {_minutes(statistics.median(medians))}"
                     f" (n={len(maxes)} runs,"
                     f" range {_minutes(min(maxes))}-{_minutes(max(maxes))})")
        else:
            line += "; baseline: none"
        lines.append(line)
    line = f"- runner-minutes (unittest + lint): {runner_minutes / 60:.0f}"
    if baselines and baselines["runner_minutes"]:
        runs = baselines["runner_minutes"]
        line += (f" vs baseline median {statistics.median(runs):.0f}"
                 f" (n={len(runs)} runs,"
                 f" range {min(runs):.0f}-{max(runs):.0f})")
    else:
        line += "; baseline: none"
    line += f"; max queue: {_minutes(max(queues) if queues else None)}"
    lines.append(line)
    return "\n".join(lines)


def section_slowest(records, top=15):
    groups = {}
    for record in records:
        key = (record["os"], record["python"])
        modules = groups.setdefault(key, {})
        for module, stats in record["modules"].items():
            wall = stats.get("wall_seconds")
            if wall is None:
                continue
            if module not in modules or wall > modules[module]["wall_seconds"]:
                modules[module] = stats
    lines = ["", "### Slowest modules", ""]
    for key in sorted(groups):
        lines.append(f"#### {key[0]}/{key[1]}")
        lines.append("")
        has_process = any(stats.get("process_seconds") is not None
                          for stats in groups[key].values())
        process_col = (" | process" if has_process else "")
        lines.append(f"| module | wall{process_col} | test | setup share"
                     " | fixture gap |")
        lines.append("| --- " * (6 if has_process else 5) + "|")
        ranked = sorted(groups[key].items(),
                        key=lambda item: item[1]["wall_seconds"], reverse=True)
        for module, stats in ranked[:top]:
            test_seconds = stats.get("test_seconds")
            setup = stats.get("setup_seconds")
            share = ("n/a" if setup is None or not test_seconds
                     else f"{100 * setup / test_seconds:.0f}%")
            cells = [_cell(module), _seconds(stats["wall_seconds"])]
            if has_process:
                cells.append(_seconds(stats.get("process_seconds")))
            cells += [_seconds(test_seconds), share,
                      _seconds(stats.get("fixture_gap_seconds"))]
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")
    return "\n".join(lines).rstrip()


def load_baselines(root):
    """Validated baseline-<run>.json combined files, newest run first.

    Returns (entries, problems); entries are {"run_id", "data"} dicts. Every
    input goes through the shard runner's combined-file validator."""
    entries, problems = [], []
    directory = Path(root)
    if not directory.is_dir():
        problems.append(f"baselines directory missing: {directory}")
        return entries, problems
    for path in sorted(directory.glob("*.json")):
        _jobs, problem = shardtools._combined_jobs(path)
        if problem is not None:
            problems.append(f"ignored baseline `{_cell(path.name)}`: {problem}")
            continue
        try:
            # The validator above already size- and format-checked the file.
            data = json.loads(path.read_bytes())
        except Exception as exc:
            problems.append(f"ignored baseline `{_cell(path.name)}`: {exc}")
            continue
        run_id = data.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            stem = path.stem
            run_id = stem.split("-", 1)[-1] if stem.startswith("baseline-") else stem
        entries.append({"run_id": run_id, "data": data})

    def newest_first(entry):
        try:
            return (0, -int(entry["run_id"]))
        except ValueError:
            return (1, entry["run_id"])

    entries.sort(key=newest_first)
    return entries, problems


def baseline_stats(entries):
    """Per-module and per-(os, python) baselines over the collected runs.

    A module's baseline is the median of its per-run macOS/3.11 wall seconds;
    runs are kept newest-first so the newest two can be checked for sustained
    drift. execs: (os, py) -> per-run max and median shard exec seconds.
    """
    module_runs, execs, runner_minutes = {}, {}, []
    for entry in entries:
        per_module = {}
        for module, wall in shardtools._combined_measurements(entry["data"]):
            per_module.setdefault(module, []).append(wall)
        for module, walls in per_module.items():
            module_runs.setdefault(module, []).append(statistics.median(walls))
        groups, total, counted = {}, 0.0, False
        for job in entry["data"].get("jobs", []):
            if not isinstance(job, dict) or job.get("name") == "timing-summary":
                continue
            os_name, python = job.get("os"), job.get("python")
            exec_seconds = _num(job.get("exec_seconds"))
            if (isinstance(os_name, str) and isinstance(python, str)
                    and exec_seconds is not None):
                groups.setdefault((os_name, python), []).append(exec_seconds)
            total_seconds = _num(job.get("total_seconds"))
            if total_seconds is not None:
                total += total_seconds
                counted = True
        for key, values in groups.items():
            group = execs.setdefault(key, {"max": [], "median": []})
            group["max"].append(max(values))
            group["median"].append(statistics.median(values))
        if counted:
            runner_minutes.append(total / 60)
    modules = {module: {"median": statistics.median(walls), "n": len(walls),
                        "min": min(walls), "max": max(walls), "runs": walls}
               for module, walls in module_runs.items()}
    return {"modules": modules, "execs": execs, "runner_minutes": runner_minutes}


def _current_module_walls(records, os_name=None, python=None):
    walls = {}
    for record in records:
        if (os_name is not None
                and (record["os"], record["python"]) != (os_name, python)):
            continue
        for module, stats in record["modules"].items():
            # Process seconds first so both sides of the baseline comparison
            # measure the same thing when the record came from a worker pool.
            wall = stats.get("process_seconds")
            if wall is None:
                wall = stats.get("wall_seconds")
            if wall is not None:
                walls[module] = max(wall, walls.get(module, 0.0))
    return walls


def _load_weights(path):
    previous = shardtools.WEIGHTS_FILE
    try:
        shardtools.WEIGHTS_FILE = Path(path)
        return shardtools.load_weights_file()
    finally:
        shardtools.WEIGHTS_FILE = previous


REFRESH_CMD = ("python3 tests/run_test_shards.py --refresh-weights "
               "<timing-combined.json ...>")


def _build_warnings(records, jobs_metrics, baselines, weights_data):
    """(warnings, notices, inconclusive) — warnings are annotatable, notices
    are informational annotations, inconclusive lines stay in the summary."""
    warnings, notices, inconclusive = [], [], []
    groups = {}
    for metrics in jobs_metrics:
        if metrics["os"] is not None and metrics["exec"] is not None:
            groups.setdefault((metrics["os"], metrics["python"]),
                              []).append(metrics["exec"])
    for (os_name, python), execs in sorted(groups.items()):
        median = statistics.median(execs)
        ratio = max(execs) / median if median else float("inf")
        if ratio > 1.5 and max(execs) - median > 180:
            warnings.append(
                f"shard imbalance {os_name}/{python}: max/median {ratio:.2f},"
                f" slowest shard {max(execs) - median:.0f}s over median"
                f" -- refresh: {REFRESH_CMD}")
    current_mac = _current_module_walls(records, "macos", "3.11")
    if baselines is not None:
        for module in sorted(current_mac):
            base = baselines["modules"].get(module)
            if base is None:
                continue
            wall = current_mac[module]
            slower = wall > 1.5 * base["median"] and wall - base["median"] > 60
            if not slower:
                continue
            detail = (f"`{_cell(module)}` {wall:.0f}s vs baseline median"
                      f" {base['median']:.0f}s (n={base['n']} runs,"
                      f" range {base['min']:.0f}s-{base['max']:.0f}s)")
            if base["n"] < 3:
                inconclusive.append(
                    f"inconclusive (n={base['n']}): slower than baseline"
                    f" {detail}")
                continue
            sustained = (base["n"] >= 5 and len(base["runs"]) >= 2 and all(
                historical > 1.5 * base["median"]
                and historical - base["median"] > 60
                for historical in base["runs"][:2]))
            warnings.append("module slower than baseline: " + detail
                            + (" -- sustained" if sustained else ""))
    if weights_data is not None:
        weights = weights_data.get("weights", {})
        samples = weights_data.get("samples", {})
        stale = shardtools.stale_estimates(weights, current_mac)
        if stale:
            warnings.append("stale weight estimates: "
                            + ", ".join(f"`{_cell(module)}`" for module in stale)
                            + f" -- refresh: {REFRESH_CMD}")
        local = sorted(module for module in weights
                       if not _num(samples.get(module)) and module in current_mac)
        if local:
            notices.append("local weight estimates measured on CI: "
                           + ", ".join(f"`{_cell(module)}`" for module in local)
                           + f" -- refresh: {REFRESH_CMD}")
        missing = sorted(set(_current_module_walls(records)) - set(weights))
        if missing:
            warnings.append("modules without a pinned weight: "
                            + ", ".join(f"`{_cell(module)}`" for module in missing)
                            + " -- run: python3 tests/run_test_shards.py"
                            + " --measure " + " ".join(missing))
    return warnings, notices, inconclusive


def section_warnings(warnings, notices, inconclusive):
    lines = ["", "### Warnings", ""]
    if not warnings and not notices and not inconclusive:
        lines.append("- none")
    for line in warnings:
        lines.append(f"- {line}")
    for line in notices:
        lines.append(f"- {line}")
    for line in inconclusive:
        lines.append(f"- {line}")
    return "\n".join(lines)


def _annotation(kind, text):
    escaped = re.sub(r"[%\r\n]", lambda m: {"%": "%25", "\r": "%0D",
                                            "\n": "%0A"}[m.group(0)], text)
    return f"::{kind} title=Test timing::{escaped.replace('`', '')}"


def section_notes(records, jobs_metrics, records_by_key, extra_notes=()):
    notes = ["", "### Measurement notes", ""]
    for record in records:
        if not record["complete"]:
            notes.append(f"- incomplete record `{_cell(record['file'])}`"
                         f" ({record['os']}/{record['python']} shard {record['shard_index']})")
        if record["truncated"]:
            notes.append(f"- truncated test rows in `{_cell(record['file'])}`")
    missing = [m["name"] for m in jobs_metrics
               if m["os"] is not None and _record_for(records_by_key, m) is None]
    if missing:
        notes.append("- jobs without records: " + ", ".join(_cell(n) for n in missing))
    for note in extra_notes:
        notes.append(f"- {_cell(note)}")
    if len(notes) == 3:
        notes.append("- none")
    return "\n".join(notes)


def combined_payload(records, jobs_metrics, records_by_key, baseline_info=None):
    revision, run_id, _ = _provenance(records)
    jobs = []
    for metrics in jobs_metrics:
        record = _record_for(records_by_key, metrics)
        jobs.append({"name": metrics["name"], "os": metrics["os"],
                     "python": metrics["python"], "shard": metrics["shard"],
                     "queue_seconds": metrics["queue"],
                     "setup_seconds": metrics["setup"],
                     "exec_seconds": metrics["exec"],
                     "total_seconds": metrics["total"],
                     "conclusion": metrics["conclusion"],
                     "record": None if record is None else {
                         "modules": record["modules"],
                         "summary": record["summary"],
                         "shard": record["shard"],
                         "complete": record["complete"]}})
    return {"format": COMBINED_FORMAT,
            "run_id": run_id,
            "revision": revision,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "baseline": baseline_info or {"runs": [], "modules_n": 0},
            "jobs": jobs}


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="ci_timing_summary.py",
        description="merge CI timing artifacts and job timings into a step summary")
    parser.add_argument("--records", required=True, metavar="DIR",
                        help="directory of downloaded timing-record artifacts")
    parser.add_argument("--jobs", required=True, metavar="FILE",
                        help="GitHub jobs API response(s), possibly concatenated")
    parser.add_argument("--combined", required=True, metavar="FILE",
                        help="write the combined JSON summary here")
    parser.add_argument("--markdown", required=True, metavar="FILE",
                        help="append the Markdown summary here")
    parser.add_argument("--baselines", metavar="DIR",
                        help="directory of baseline-<run>.json combined files "
                             "from recent main runs")
    parser.add_argument("--weights", metavar="FILE",
                        help="tests/shard_weights.json for drift checks")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    records, ignored = load_records(args.records)
    try:
        jobs = load_jobs(args.jobs)
    except (OSError, ValueError) as exc:
        jobs = []
        ignored.append({"file": str(args.jobs), "reason": f"unreadable jobs file: {type(exc).__name__}"})
    jobs_metrics = [job_metrics(job) for job in jobs]
    records_by_key = {}
    for record in records:
        key = (record["os"], record["python"], record["shard_index"])
        if key[2] is None:
            continue
        # Prefer a complete record when a key appears twice.
        if key not in records_by_key or (record["complete"] and not records_by_key[key]["complete"]):
            records_by_key[key] = record
    extra_notes, baselines, baseline_runs = [], None, []
    if args.baselines:
        entries, problems = load_baselines(args.baselines)
        extra_notes.extend(problems)
        if entries:
            baselines = baseline_stats(entries)
            baseline_runs = [entry["run_id"] for entry in entries]
        else:
            extra_notes.append("no usable baselines")
    weights_data = None
    if args.weights:
        try:
            weights_data = _load_weights(args.weights)
        except Exception as exc:
            extra_notes.append(
                f"weights file unreadable: {type(exc).__name__}")
    warnings, notices, inconclusive = [], [], []
    try:
        warnings, notices, inconclusive = _build_warnings(
            records, jobs_metrics, baselines, weights_data)
    except Exception as exc:
        extra_notes.append(f"warnings unavailable: {type(exc).__name__}")
    for kind, line in ([("warning", line) for line in warnings]
                       + [("notice", line) for line in notices])[:10]:
        print(_annotation(kind, line))
    sections = []
    for title, builder in (("header", lambda: section_header(records, ignored, jobs_metrics)),
                           ("warnings", lambda: section_warnings(warnings, notices, inconclusive)),
                           ("jobs", lambda: section_jobs(jobs_metrics, records_by_key)),
                           ("imbalance", lambda: section_imbalance(jobs_metrics, baselines)),
                           ("slowest", lambda: section_slowest(records)),
                           ("notes", lambda: section_notes(records, jobs_metrics, records_by_key, extra_notes))):
        try:
            sections.append(builder())
        except Exception as exc:
            sections.append(f"\n### {title}\n\nnot available: {type(exc).__name__}")
    try:
        markdown_path = Path(args.markdown)
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        with open(markdown_path, "a") as handle:
            handle.write("\n".join(sections) + "\n")
    except OSError as exc:
        print(f"ci_timing_summary: markdown not written: {type(exc).__name__}", file=sys.stderr)
    try:
        combined_path = Path(args.combined)
        combined_path.parent.mkdir(parents=True, exist_ok=True)
        combined_path.write_text(
            json.dumps(combined_payload(
                records, jobs_metrics, records_by_key,
                {"runs": [str(run)[:100] for run in baseline_runs],
                 "modules_n": len(baselines["modules"]) if baselines else 0}),
                indent=1, sort_keys=True) + "\n")
    except Exception as exc:
        print(f"ci_timing_summary: combined record not written: {type(exc).__name__}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
