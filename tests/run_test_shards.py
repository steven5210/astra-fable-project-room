"""Shard the Project Room offline suite deterministically by module: the module
set is the sorted stems of every top-level test*.py file, identical to what
`python -m unittest discover` finds. Modules are partitioned across shards by
longest-processing-time greedy assignment over the measured per-module seconds
in tests/shard_weights.json; a module missing from the file takes the median
weight, and every module weighs 1.0 when the file is absent. CI runs one shard
per job with `--shard I --shards N`; `--jobs J` runs all J shards concurrently
on a developer machine and writes each shard's combined output to a log file.
"""
import argparse
import json
import re
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAN_RE = re.compile(r"^Ran (\d+) tests? in ([\d.]+)s$")
RESULT_RE = re.compile(r"^(OK|FAILED)(?: \(.*\))?$")


def test_modules():
    return sorted(path.stem for path in ROOT.glob("test*.py") if path.is_file())


def load_weights():
    weights_file = ROOT / "tests" / "shard_weights.json"
    if not weights_file.is_file():
        return {}
    return dict(json.loads(weights_file.read_text())["weights"])


def assign(modules, shards, weights):
    default = statistics.median(weights.values()) if weights else 1.0
    weight = lambda module: float(weights.get(module, default))
    groups = [[] for _ in range(shards)]
    loads = [0.0] * shards
    for module in sorted(modules, key=lambda m: (-weight(m), m)):
        target = min(range(shards), key=lambda i: (loads[i], i))
        groups[target].append(module)
        loads[target] += weight(module)
    for group in groups:
        group.sort()
    return groups, loads


def unittest_cmd(modules, verbose):
    cmd = [sys.executable, "-B", "-m", "unittest"]
    if verbose:
        cmd.append("-v")
    return cmd + modules


def parse_args(argv):
    parser = argparse.ArgumentParser(prog="run_test_shards.py")
    parser.add_argument("--shard", type=int, metavar="I", help="run shard index I")
    parser.add_argument("--shards", type=int, default=4, metavar="N",
                        help="split the modules into N shards (default: 4)")
    parser.add_argument("--jobs", type=int, metavar="J",
                        help="run all J shards concurrently")
    parser.add_argument("--log-dir", metavar="DIR",
                        help="directory for --jobs shard logs")
    parser.add_argument("--list", action="store_true",
                        help="print each shard's modules and exit")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="pass -v to unittest")
    args = parser.parse_args(argv)
    if args.list + (args.shard is not None) + (args.jobs is not None) != 1:
        parser.exit(2, "error: choose exactly one of --list, --shard or --jobs\n")
    return args


def log_summary(log_path):
    ran_line, result_line, count = "-", "-", 0
    for line in reversed(log_path.read_text().splitlines()):
        ran = RAN_RE.match(line)
        if ran_line == "-" and ran:
            ran_line, count = line, int(ran.group(1))
        if result_line == "-" and RESULT_RE.match(line):
            result_line = line
        if ran_line != "-" and result_line != "-":
            break
    return ran_line, result_line, count


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
    running = []
    for i, group in enumerate(groups):
        log_path = log_dir / f"shard-{i}.log"
        handle = open(log_path, "w")
        proc = subprocess.Popen(unittest_cmd(group, args.verbose), cwd=ROOT,
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
    modules = test_modules()
    shards = args.jobs if args.jobs is not None else args.shards
    if shards < 1:
        print(f"error: shard count must be >= 1 (got {shards})", file=sys.stderr)
        return 2
    groups, loads = assign(modules, shards, load_weights())
    assert sorted(m for group in groups for m in group) == modules, \
        "shard assignment must cover every module exactly once"
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
        proc = subprocess.run(unittest_cmd(groups[args.shard], args.verbose),
                              cwd=ROOT)
        return proc.returncode
    return run_all(args, groups)


if __name__ == "__main__":
    sys.exit(main())
