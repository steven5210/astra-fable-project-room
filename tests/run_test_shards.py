"""Shard the Project Room offline suite deterministically by module: the module
set is the sorted stems of every top-level test*.py file, identical to what
`python -m unittest discover` finds. Modules are partitioned across shards by
longest-processing-time greedy assignment over the pinned per-module seconds
in tests/shard_weights.json (format project_room_shard_weights_v2); every
discovered module must have a pinned weight -- a new module needs one estimate
from `--measure MODULE` until the next CI refresh replaces it. CI runs one
shard per job with `--shard I --shards N`; `--jobs J` runs all J shards
concurrently on a developer machine and writes each shard's combined output to
a log file. `--record DIR` runs each shard through tests/timed_unittest.py
instead of `python -m unittest` and writes a per-test timing record to
DIR/shard-I-of-N.json with the shard index in PROJECT_ROOM_TIMING_SHARD.
`--check-weights` verifies coverage, `--refresh-weights FILE...` rewrites the
weights from timing-summary combined artifacts, and `--measure MODULE...`
pins a local estimate for new modules. `--workers K` (with `--shard` or
`--jobs`) runs each shard's modules through up to K worker processes, longest
module first, and merges the per-module timing records into the shard record;
the default K=1 keeps the single-process shard unchanged.
"""
import argparse
import importlib.util
import json
import math
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAN_RE = re.compile(r"^Ran (\d+) tests? in ([\d.]+)s$")
RESULT_RE = re.compile(r"^(OK|FAILED)(?: \(.*\))?$")
WEIGHTS_FILE = ROOT / "tests" / "shard_weights.json"
WEIGHTS_FORMAT = "project_room_shard_weights_v2"
COMBINED_FORMAT = "project_room_timing_combined_v1"
MAX_COMBINED_BYTES = 8 * 1024 * 1024
REFRESH_PLATFORM = ("macos", "3.11")


class WeightsFileError(Exception):
    """The weights file is unreadable or not format v2."""


class MissingWeights(Exception):
    """Discovered modules without a pinned weight / weighted non-modules."""

    def __init__(self, missing, orphaned):
        self.missing = list(missing)
        self.orphaned = list(orphaned)
        super().__init__(f"{len(self.missing)} discovered module(s) "
                         f"without a pinned weight")


def test_modules():
    return sorted(path.stem for path in ROOT.glob("test*.py") if path.is_file())


def load_weights_file():
    try:
        data = json.loads(WEIGHTS_FILE.read_text())
    except (OSError, ValueError) as exc:
        raise WeightsFileError(f"cannot read {WEIGHTS_FILE}: {exc}")
    if (not isinstance(data, dict) or not isinstance(data.get("_meta"), dict)
            or not isinstance(data.get("weights"), dict)
            or data["_meta"].get("format") != WEIGHTS_FORMAT):
        raise WeightsFileError(
            f"{WEIGHTS_FILE} is not a {WEIGHTS_FORMAT} file; produce one with: "
            f"python3 tests/run_test_shards.py --refresh-weights "
            f"timing-combined.json [...]")
    return data


def load_weights():
    return dict(load_weights_file()["weights"])


def write_weights_file(data):
    tmp = WEIGHTS_FILE.with_name(WEIGHTS_FILE.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    os.replace(tmp, WEIGHTS_FILE)


def _missing_text(error):
    lines = []
    if error.missing:
        lines.append("error: discovered test modules without a pinned weight: "
                     + ", ".join(error.missing))
        lines.append("run: python3 tests/run_test_shards.py --measure "
                     + " ".join(error.missing))
    if error.orphaned:
        lines.append("warning: weighted entries that are not discovered test "
                     "modules (stale; --refresh-weights drops them): "
                     + ", ".join(error.orphaned))
        lines.append("run: python3 tests/run_test_shards.py --check-weights")
    return "\n".join(lines)


def assign(modules, shards, weights):
    missing = sorted(set(modules) - set(weights))
    orphaned = sorted(set(weights) - set(modules))
    if missing:
        raise MissingWeights(missing, orphaned)
    groups = [[] for _ in range(shards)]
    loads = [0.0] * shards
    for module in sorted(modules, key=lambda m: (-float(weights[m]), m)):
        target = min(range(shards), key=lambda i: (loads[i], i))
        groups[target].append(module)
        loads[target] += float(weights[module])
    for group in groups:
        group.sort()
    return groups, loads


def stale_estimates(weights, measured):
    """Modules whose measured seconds differ from the pinned weight by more
    than a factor of 2 and more than 30 s (single-run noise runs 30-56 %, so
    anything tighter would flag noise)."""
    stale = []
    for module, weight in weights.items():
        actual = measured.get(module)
        if actual is None:
            continue
        weight, actual = float(weight), float(actual)
        if abs(actual - weight) > 30 and (actual > 2 * weight
                                        or weight > 2 * actual):
            stale.append(module)
    return sorted(stale)


def unittest_cmd(modules, verbose, record=None):
    if record is None:
        cmd = [sys.executable, "-B", "-m", "unittest"]
        if verbose:
            cmd.append("-v")
        return cmd + modules
    # Run the timing runner by path: a regular `tests` package on sys.path
    # would shadow the namespace package that `-m tests.timed_unittest` needs.
    cmd = [sys.executable, "-B", str(ROOT / "tests" / "timed_unittest.py"),
           "--record", str(record)]
    if verbose:
        cmd.append("-v")
    return cmd + modules


def _timed_unittest():
    spec = importlib.util.spec_from_file_location(
        "timed_unittest", ROOT / "tests" / "timed_unittest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_weights():
    data = load_weights_file()
    weights, samples = data["weights"], data.get("samples", {})
    modules = test_modules()
    missing = sorted(set(modules) - set(weights))
    orphaned = sorted(set(weights) - set(modules))
    if missing or orphaned:
        print(_missing_text(MissingWeights(missing, orphaned)), file=sys.stderr)
        return 2
    local = sorted(module for module in weights
                   if not _num(samples.get(module)))
    print(f"weights: {len(weights)} modules covered, "
          f"{len(local)} local estimates (samples 0): {local}")
    return 0


def _num(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def measure(modules_to_measure):
    discovered = test_modules()
    unknown = sorted(set(modules_to_measure) - set(discovered))
    if unknown:
        print(f"error: not discovered test modules: {', '.join(unknown)}",
              file=sys.stderr)
        return 2
    data = load_weights_file()
    data.setdefault("samples", {})
    for module in modules_to_measure:
        with tempfile.TemporaryDirectory() as tmp:
            record_path = Path(tmp) / "record.json"
            proc = subprocess.run(unittest_cmd([module], False, record_path),
                                  cwd=ROOT)
            wall = None
            if proc.returncode == 0 and record_path.is_file():
                try:
                    record = json.loads(record_path.read_text())
                    stats = record["modules"][module]
                    if record.get("complete") is True:
                        wall = _num(stats.get("wall_seconds"))
                except (OSError, ValueError, KeyError, TypeError):
                    wall = None
            if wall is None:
                print(f"error: {module} produced no usable timing record "
                      f"(exit {proc.returncode}); weights file unchanged",
                      file=sys.stderr)
                return 1
        data["weights"][module] = round(wall, 2)
        data["samples"][module] = 0
        write_weights_file(data)
        print(f"{module}: {wall} s (local estimate, samples 0)")
    return 0


def _combined_jobs(path):
    """Untrusted combined input: bounded, format- and type-checked."""
    path = Path(path)
    try:
        if path.stat().st_size > MAX_COMBINED_BYTES:
            return None, "over 8 MiB"
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None, "unreadable json"
    if not isinstance(data, dict) or data.get("format") != COMBINED_FORMAT:
        return None, f"not a {COMBINED_FORMAT} file"
    if not isinstance(data.get("jobs"), list):
        return None, "jobs is not a list"
    return data, None


def _combined_job_qualifies(job):
    if not isinstance(job, dict):
        return False
    record = job.get("record")
    return ((job.get("os"), job.get("python")) == REFRESH_PLATFORM
            and isinstance(record, dict)
            and record.get("complete") is True
            and isinstance(record.get("modules"), dict))


def _combined_measurements(data):
    """Yield (module, wall_seconds) from complete macos/3.11 jobs."""
    for job in data["jobs"]:
        if not _combined_job_qualifies(job):
            continue
        for module, stats in job["record"]["modules"].items():
            if not isinstance(module, str) or not isinstance(stats, dict):
                continue
            wall = _num(stats.get("wall_seconds"))
            if wall is not None:
                yield module, wall


def refresh_weights(paths):
    data = load_weights_file()
    weights, samples = data["weights"], data.setdefault("samples", {})
    measured = {}
    sources = []
    for path in paths:
        combined, problem = _combined_jobs(path)
        if combined is None:
            print(f"ignored {path}: {problem}", file=sys.stderr)
            continue
        jobs_seen = sum(1 for job in combined["jobs"]
                        if _combined_job_qualifies(job))
        for module, wall in _combined_measurements(combined):
            measured.setdefault(module, []).append(wall)
        run_id = combined.get("run_id")
        revision = combined.get("revision")
        sources.append(
            f"run {run_id if isinstance(run_id, str) else '?'} "
            f"({(revision or '')[:7] if isinstance(revision, str) else '?'}, "
            f"{REFRESH_PLATFORM[0]}-latest/{REFRESH_PLATFORM[1]}, "
            f"{jobs_seen} jobs)")
    modules = test_modules()
    new_weights, new_samples, kept = {}, {}, []
    for module in modules:
        walls = measured.get(module)
        if walls:
            new_weights[module] = round(statistics.median(walls), 2)
            new_samples[module] = len(walls)
        elif module in weights:
            new_weights[module] = weights[module]
            new_samples[module] = samples.get(module, 0)
            kept.append(module)
    missing = sorted(set(modules) - set(new_weights))
    dropped = sorted(set(weights) - set(modules))
    if missing:
        print(_missing_text(MissingWeights(missing, dropped)), file=sys.stderr)
        return 2
    data["_meta"]["sources"] = sources
    data["_meta"]["refreshed_at"] = datetime.now(timezone.utc).date().isoformat()
    data["weights"], data["samples"] = new_weights, new_samples
    changes = []
    for module in sorted(new_weights):
        old = weights.get(module)
        if old == new_weights[module]:
            continue
        changes.append((abs(new_weights[module] - old) if old is not None
                        else float("inf"), module, old,
                       new_weights[module], new_samples[module]))
    changes.sort(reverse=True)
    print(f"{'module':<58}{'old':>10}{'new':>10}{'samples':>9}{'change':>9}")
    for _, module, old, new, n in changes[:20]:
        old_text = f"{old:>9.1f}" if old is not None else f"{'—':>9}"
        pct = ("new" if old in (None, 0)
               else f"{100 * (new - old) / old:+.0f}%")
        print(f"{module:<58}{old_text}{new:>10.1f}{n:>9}{pct:>9}")
    print(f"kept previous values (no measurement): {len(kept)} modules")
    if kept:
        print("  " + " ".join(kept))
    print(f"dropped stale entries (not discovered): {len(dropped)}")
    if dropped:
        print("  " + " ".join(dropped))
    write_weights_file(data)
    return 0


def parse_args(argv):
    parser = argparse.ArgumentParser(prog="run_test_shards.py")
    parser.add_argument("--shard", type=int, metavar="I", help="run shard index I")
    parser.add_argument("--shards", type=int, default=5, metavar="N",
                        help="split the modules into N shards (default: 5)")
    parser.add_argument("--jobs", type=int, metavar="J",
                        help="run all J shards concurrently")
    parser.add_argument("--log-dir", metavar="DIR",
                        help="directory for --jobs shard logs")
    parser.add_argument("--record", metavar="DIR",
                        help="write a per-test timing record per shard to DIR")
    parser.add_argument("--list", action="store_true",
                        help="print each shard's modules and exit")
    parser.add_argument("--check-weights", action="store_true",
                        help="verify every discovered module has a weight")
    parser.add_argument("--measure", nargs="+", metavar="MODULE",
                        help="pin MODULE weights from one local run (samples 0)")
    parser.add_argument("--refresh-weights", nargs="+", metavar="FILE",
                        help="refresh weights from timing-summary combined files")
    parser.add_argument("--workers", type=int, default=1, metavar="K",
                        help="run each shard's modules through up to K worker "
                             "processes (with --shard or --jobs; default: 1)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="pass -v to unittest")
    args = parser.parse_args(argv)
    modes = [args.list, args.shard is not None, args.jobs is not None,
             args.check_weights, args.measure is not None,
             args.refresh_weights is not None]
    if modes.count(True) != 1:
        parser.exit(2, "error: choose exactly one of --list, --shard, --jobs, "
                    "--check-weights, --measure or --refresh-weights\n")
    if args.workers < 1:
        parser.exit(2, "error: --workers must be >= 1\n")
    if args.workers > 1 and args.shard is None and args.jobs is None:
        parser.exit(2, "error: --workers is only valid with --shard or --jobs\n")
    return args


def log_summary(log_path):
    ran_line, result_line, count = "-", "-", 0
    for line in reversed(log_path.read_text(errors="replace").splitlines()):
        ran = RAN_RE.match(line)
        if ran_line == "-" and ran:
            ran_line, count = line, int(ran.group(1))
        if result_line == "-" and RESULT_RE.match(line):
            result_line = line
        if ran_line != "-" and result_line != "-":
            break
    return ran_line, result_line, count


def run_shard_workers(index, shards, group, weights, args):
    """Run one shard's modules through up to ``args.workers`` child processes.

    The queue is longest-module-first by pinned weight; each child's full
    output is replayed as one block in completion order, and with ``--record``
    the per-module records (under ``DIR/workers/shard-I-of-N/``) are merged
    into the shard record at ``DIR/shard-I-of-N.json``.
    """
    queue = sorted(group, key=lambda m: (-float(weights[m]), m))
    workers_dir = record_dir = None
    if args.record:
        record_dir = Path(args.record)
        workers_dir = record_dir / "workers" / f"shard-{index}-of-{shards}"
        workers_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PROJECT_ROOM_TIMING_SHARD=f"{index}/{shards}")
    log_dir = Path(tempfile.mkdtemp(
        prefix=f"project-room-shard-{index}-workers-"))
    started = time.monotonic()
    pending, running, finished = list(queue), {}, {}
    while pending or running:
        harvested = False
        for proc, (module, log_path, handle,
                   child_started) in list(running.items()):
            code = proc.poll()
            if code is None:
                continue
            handle.close()
            del running[proc]
            seconds = time.monotonic() - child_started
            finished[module] = (code, seconds, log_path)
            print(f"=== {module}: exit {code}, {seconds:.1f}s ===", flush=True)
            out = getattr(sys.stdout, "buffer", None)
            if out is not None:
                out.write(log_path.read_bytes())
                out.flush()
            else:
                sys.stdout.write(log_path.read_text(errors="replace"))
            sys.stdout.flush()
            harvested = True
        while pending and len(running) < args.workers:
            module = pending.pop(0)
            log_path = log_dir / f"{module}.log"
            record = (workers_dir / f"{module}.json"
                      if workers_dir is not None else None)
            handle = open(log_path, "wb")
            proc = subprocess.Popen(
                unittest_cmd([module], args.verbose, record),
                cwd=ROOT, env=env,
                stdout=handle, stderr=subprocess.STDOUT)
            running[proc] = (module, log_path, handle, time.monotonic())
        if not harvested:
            time.sleep(0.1)
    if record_dir is not None:
        timed = _timed_unittest()
        records, worker_runs = [], []
        for module in queue:
            code, seconds, _ = finished[module]
            record = None
            path = workers_dir / f"{module}.json"
            if path.is_file():
                try:
                    record = json.loads(path.read_text())
                except (OSError, ValueError):
                    record = None
            worker_runs.append({"module": module, "exit_code": code,
                                "seconds": round(seconds, 3),
                                "record": record is not None})
            records.append(record)
        timed.write_record(
            record_dir / f"shard-{index}-of-{shards}.json",
            timed.merge_records(records,
                                wall_seconds=time.monotonic() - started,
                                modules_requested=list(group),
                                workers=args.workers,
                                worker_runs=worker_runs))
    bad = total = 0
    print(f"{'module':<40}{'ran':<33}{'result':<22}{'exit':<6}log")
    for module in sorted(group):
        code, seconds, log_path = finished[module]
        ran_line, result_line, count = log_summary(log_path)
        total += count
        if code != 0 or ran_line == "-":
            bad += 1
        print(f"{module:<40}{ran_line:<33}{result_line:<22}"
              f"{code:<6}{log_path}")
    print(f"total: {total} tests")
    return 1 if bad else 0


def run_all(args, groups):
    empty = [i for i, group in enumerate(groups) if not group]
    if empty:
        print(f"error: shards {empty} would be empty "
              f"({sum(map(len, groups))} modules over {len(groups)} shards)",
              file=sys.stderr)
        return 2
    if args.log_dir:
        log_dir = Path(args.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
    else:
        log_dir = Path(tempfile.mkdtemp(prefix="project-room-test-shards-"))
    record_dir = None
    if args.record:
        record_dir = Path(args.record)
        record_dir.mkdir(parents=True, exist_ok=True)
    running = []
    for i, group in enumerate(groups):
        log_path = log_dir / f"shard-{i}.log"
        handle = open(log_path, "w")
        if args.workers >= 2:
            # Each shard job runs its own bounded worker pool internally.
            cmd = [sys.executable, "-B",
                   str(ROOT / "tests" / "run_test_shards.py"),
                   "--shard", str(i), "--shards", str(len(groups)),
                   "--workers", str(args.workers)]
            if record_dir is not None:
                cmd += ["--record", str(record_dir)]
            if args.verbose:
                cmd.append("-v")
            proc = subprocess.Popen(cmd, cwd=ROOT,
                                    stdout=handle, stderr=subprocess.STDOUT)
        else:
            record, env = None, None
            if record_dir is not None:
                record = record_dir / f"shard-{i}-of-{len(groups)}.json"
                env = dict(os.environ,
                           PROJECT_ROOM_TIMING_SHARD=f"{i}/{len(groups)}")
            proc = subprocess.Popen(unittest_cmd(group, args.verbose, record),
                                    cwd=ROOT, env=env,
                                    stdout=handle, stderr=subprocess.STDOUT)
        running.append((i, group, log_path, handle, proc))
    bad = total = 0
    print(f"{'shard':<6}{'modules':<9}{'ran':<33}{'result':<22}{'exit':<6}log")
    for i, group, log_path, handle, proc in running:
        code = proc.wait()
        handle.close()
        ran_line, result_line, count = log_summary(log_path)
        total += count
        if code != 0 or ran_line == "-":
            bad += 1
        print(f"{i:<6}{len(group):<9}{ran_line:<33}{result_line:<22}{code:<6}{log_path}")
    print(f"total: {total} tests")
    return 1 if bad else 0


def main(argv=None):
    args = parse_args(argv)
    try:
        if args.check_weights:
            return check_weights()
        if args.measure is not None:
            return measure(args.measure)
        if args.refresh_weights is not None:
            return refresh_weights(args.refresh_weights)
        weights = load_weights()
    except WeightsFileError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    modules = test_modules()
    shards = args.jobs if args.jobs is not None else args.shards
    if shards < 1:
        print(f"error: shard count must be >= 1 (got {shards})", file=sys.stderr)
        return 2
    try:
        groups, loads = assign(modules, shards, weights)
    except MissingWeights as exc:
        print(_missing_text(exc), file=sys.stderr)
        return 2
    assert sorted(m for group in groups for m in group) == modules, \
        "shard assignment must cover every module exactly once"
    orphaned = sorted(set(weights) - set(modules))
    if orphaned:
        print(f"warning: weighted entries that are not discovered test "
              f"modules (stale; --refresh-weights drops them): "
              f"{', '.join(orphaned)}", file=sys.stderr)
    if args.list:
        for i, group in enumerate(groups):
            print(f"shard {i}/{shards} ({len(group)} modules, "
                  f"~{loads[i] / 60:.1f} min): {' '.join(group)}")
        print(f"total: {len(modules)} modules")
        return 0
    if args.shard is not None:
        if not 0 <= args.shard < shards:
            print(f"error: shard {args.shard} out of range for {shards} shards",
                  file=sys.stderr)
            return 2
        if not groups[args.shard]:
            print(f"error: shard {args.shard} is empty "
                  f"({len(modules)} modules over {shards} shards)", file=sys.stderr)
            return 2
        if args.workers >= 2:
            return run_shard_workers(args.shard, shards, groups[args.shard],
                                     weights, args)
        record, env = None, None
        if args.record:
            record_dir = Path(args.record)
            record_dir.mkdir(parents=True, exist_ok=True)
            record = record_dir / f"shard-{args.shard}-of-{shards}.json"
            env = dict(os.environ,
                       PROJECT_ROOM_TIMING_SHARD=f"{args.shard}/{shards}")
        proc = subprocess.run(unittest_cmd(groups[args.shard], args.verbose,
                                           record), cwd=ROOT, env=env)
        return proc.returncode
    return run_all(args, groups)


if __name__ == "__main__":
    sys.exit(main())
