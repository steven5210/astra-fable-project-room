"""unittest runner that records monotonic per-test and per-module durations and
writes one JSON timing record. tests/run_test_shards.py invokes this instead of
`python -m unittest` when timing records are requested; console output keeps the
unchanged TextTestRunner format that run_test_shards parses (the per-test lines,
`Ran N tests in X.XXXs`, `OK` / `FAILED (...)`).

    python -B tests/timed_unittest.py --record PATH [-v] MODULE [MODULE...]

The module runs by path rather than `-m tests.timed_unittest` because a regular
`tests` package elsewhere on sys.path would shadow the namespace package; the
repo root is put on sys.path below so the test modules import exactly as under
`python -m unittest`.

The record carries format "project_room_test_timing_v1": run provenance
(platform, Python, revision/run id and shard index from the environment),
per-test rows with setup/body/teardown phase seconds, and per-module aggregates
whose fixture gap is the module wall time minus the summed test seconds. The
record is written atomically even when the run is interrupted (complete: false).
"""
import argparse
import contextlib
import json
import os
import platform
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if sys.path and Path(sys.path[0]).resolve() == ROOT / "tests":
    # Run by path: sys.path[0] is the tests directory; python -m unittest puts
    # the repo root there instead.
    sys.path[0] = str(ROOT)
elif str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FORMAT = "project_room_test_timing_v1"
MAX_TEST_ROWS = 20000
ID_CLIP = 300
_PHASE_HOOKS = {"_callSetUp": "setup_seconds",
                "_callTestMethod": "body_seconds",
                "_callTearDown": "teardown_seconds"}
# Stack of per-run phase stores; the innermost in-process run owns the top, so
# nested TimedTextTestRunner runs record their own ids and leave the outer
# record untouched.
_ACTIVE_PHASES = []


def _os_name():
    system = platform.system().lower()
    return "macos" if system == "darwin" else system


_FAILED_PREFIX = "unittest.loader._FailedTest."


def _module_of(test_id):
    # An import failure runs as unittest.loader._FailedTest.<requested name>;
    # attribute it to the requested module.
    if test_id.startswith(_FAILED_PREFIX):
        return test_id[len(_FAILED_PREFIX):]
    return test_id.split(".", 1)[0]


def _class_of(test_id):
    parts = test_id.split(".")
    return parts[-2][:ID_CLIP] if len(parts) >= 3 else None


def _phase_wrapper(name, original):
    field = _PHASE_HOOKS[name]

    def timed(self, *args, **kwargs):
        started = time.monotonic()
        try:
            return original(self, *args, **kwargs)
        finally:
            if _ACTIVE_PHASES:
                phases = _ACTIVE_PHASES[-1]["phases"].setdefault(self.id(), {})
                phases[field] = phases.get(field, 0.0) + (time.monotonic() - started)

    timed._project_room_phase_patch = True
    return timed


@contextlib.contextmanager
def _phase_timing():
    hooks_ok = all(hasattr(unittest.TestCase, name) for name in _PHASE_HOOKS)
    ctx = {"phases": {}, "hooks": hooks_ok}
    _ACTIVE_PHASES.append(ctx)
    originals = {}
    try:
        if hooks_ok:
            for name in _PHASE_HOOKS:
                current = getattr(unittest.TestCase, name)
                if getattr(current, "_project_room_phase_patch", False):
                    # An outer in-process run already wrapped the hook; the
                    # innermost stack entry still attributes the phases.
                    continue
                originals[name] = current
                setattr(unittest.TestCase, name, _phase_wrapper(name, current))
        yield
    finally:
        for name, original in originals.items():
            setattr(unittest.TestCase, name, original)
        popped = _ACTIVE_PHASES.pop()
        assert popped is ctx


def _new_module_stat():
    return {"tests": 0, "test_seconds": 0.0, "setup_seconds": 0.0,
            "body_seconds": 0.0, "teardown_seconds": 0.0, "phases_ok": True,
            "outcomes": {}}


class TimedTextTestResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.timed_rows = []
        self.timed_truncated = False
        self.module_stats = {}
        self.module_first = {}
        self.module_last = {}
        self._starts = {}
        self._outcomes = {}

    def _record_outcome(self, test, outcome):
        current = self._outcomes.get(test.id())
        if current in ("fail", "error") and outcome not in ("fail", "error"):
            return
        self._outcomes[test.id()] = outcome

    def addSuccess(self, test):
        self._record_outcome(test, "ok")
        super().addSuccess(test)

    def addFailure(self, test, err):
        self._record_outcome(test, "fail")
        super().addFailure(test, err)

    def addError(self, test, err):
        self._record_outcome(test, "error")
        super().addError(test, err)

    def addSkip(self, test, reason):
        self._record_outcome(test, "skip")
        super().addSkip(test, reason)

    def addExpectedFailure(self, test, err):
        self._record_outcome(test, "expected_failure")
        super().addExpectedFailure(test, err)

    def addUnexpectedSuccess(self, test):
        self._record_outcome(test, "unexpected_success")
        super().addUnexpectedSuccess(test)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self._record_outcome(
                test, "fail" if issubclass(err[0], test.failureException) else "error")
        super().addSubTest(test, subtest, err)

    def startTest(self, test):
        started = time.monotonic()
        self._starts[test.id()] = started
        self.module_first.setdefault(_module_of(test.id()), started)
        super().startTest(test)

    def stopTest(self, test):
        try:
            super().stopTest(test)
        finally:
            finished = time.monotonic()
            test_id = test.id()
            started = self._starts.pop(test_id, finished)
            module = _module_of(test_id)
            self.module_last[module] = finished
            if _ACTIVE_PHASES and _ACTIVE_PHASES[-1]["hooks"]:
                # Pop, so a test id that runs twice in one process (the
                # duplicate-discovery shape) gets two independent rows.
                phases = _ACTIVE_PHASES[-1]["phases"].pop(test_id, {})
            else:
                phases = None
            outcome = self._outcomes.pop(test_id, "ok")
            stat = self.module_stats.setdefault(module, _new_module_stat())
            stat["tests"] += 1
            stat["test_seconds"] += finished - started
            stat["outcomes"][outcome] = stat["outcomes"].get(outcome, 0) + 1
            if phases is None:
                stat["phases_ok"] = False
                row_setup = row_body = row_teardown = None
            else:
                stat["setup_seconds"] += phases.get("setup_seconds", 0.0)
                stat["body_seconds"] += phases.get("body_seconds", 0.0)
                stat["teardown_seconds"] += phases.get("teardown_seconds", 0.0)
                row_setup = round(phases.get("setup_seconds", 0.0), 3)
                row_body = round(phases.get("body_seconds", 0.0), 3)
                row_teardown = round(phases.get("teardown_seconds", 0.0), 3)
            if len(self.timed_rows) < MAX_TEST_ROWS:
                self.timed_rows.append({
                    "id": test_id[:ID_CLIP],
                    "module": module,
                    "class": _class_of(test_id),
                    "outcome": outcome,
                    "seconds": round(finished - started, 3),
                    "setup_seconds": row_setup,
                    "body_seconds": row_body,
                    "teardown_seconds": row_teardown})
            else:
                self.timed_truncated = True


class TimedTextTestRunner(unittest.TextTestRunner):
    resultclass = TimedTextTestResult

    def _makeResult(self):
        self.last_result = super()._makeResult()
        return self.last_result

    def run(self, test):
        with _phase_timing():
            return super().run(test)


def load_suite(modules):
    return unittest.TestLoader().loadTestsFromNames(modules)


def _shard_env():
    raw = os.environ.get("PROJECT_ROOM_TIMING_SHARD")
    if not raw:
        return None
    index, sep, count = raw.partition("/")
    try:
        return {"index": int(index), "count": int(count)}
    except ValueError:
        return None


def _summary(result):
    if result is None:
        return {"ran": 0, "failures": 0, "errors": 0, "skipped": 0,
                "expected_failures": 0, "unexpected_successes": 0,
                "was_successful": False}
    return {"ran": result.testsRun,
            "failures": len(result.failures),
            "errors": len(result.errors),
            "skipped": len(result.skipped),
            "expected_failures": len(result.expectedFailures),
            "unexpected_successes": len(result.unexpectedSuccesses),
            "was_successful": result.wasSuccessful()}


def build_record(result, modules_requested, started_at, wall_seconds, complete):
    modules = {}
    if result is not None:
        for name in sorted(result.module_stats):
            stats = result.module_stats[name]
            wall = result.module_last[name] - result.module_first[name]
            phases_ok = stats["phases_ok"]
            test_seconds = stats["test_seconds"]
            modules[name] = {
                "tests": stats["tests"],
                "wall_seconds": round(wall, 3),
                "test_seconds": round(test_seconds, 3),
                "setup_seconds": round(stats["setup_seconds"], 3) if phases_ok else None,
                "body_seconds": round(stats["body_seconds"], 3) if phases_ok else None,
                "teardown_seconds": round(stats["teardown_seconds"], 3) if phases_ok else None,
                "fixture_gap_seconds": round(max(0.0, wall - test_seconds), 3),
                "outcomes": dict(sorted(stats["outcomes"].items()))}
    record = {"format": FORMAT,
              "complete": bool(complete),
              "python": platform.python_version(),
              "python_minor": ".".join(map(str, sys.version_info[:2])),
              "implementation": platform.python_implementation(),
              "os": _os_name(),
              "platform": platform.platform(),
              "machine": platform.machine(),
              "revision": os.environ.get("PROJECT_ROOM_TIMING_REVISION"),
              "run_id": os.environ.get("PROJECT_ROOM_TIMING_RUN_ID"),
              "started_at": started_at,
              "shard": _shard_env(),
              "modules_requested": list(modules_requested),
              "wall_seconds": round(wall_seconds, 3),
              "summary": _summary(result),
              "modules": modules,
              "tests": list(result.timed_rows) if result is not None else []}
    if result is not None and result.timed_truncated:
        record["tests_truncated"] = True
    return record


def write_record(path, record):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n")
    os.replace(tmp, path)


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="timed_unittest.py",
        description="run unittest modules and write a per-test timing record")
    parser.add_argument("--record", required=True, metavar="PATH",
                        help="write the JSON timing record to PATH")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="pass -v verbosity to the test runner")
    parser.add_argument("modules", nargs="+", metavar="MODULE")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()
    runner = TimedTextTestRunner(verbosity=2 if args.verbose else 1,
                               warnings="default" if not sys.warnoptions else None)
    complete = False
    code = 1
    try:
        result = runner.run(load_suite(args.modules))
        complete = True
        if result.testsRun == 0:
            code = 5
        elif result.wasSuccessful():
            code = 0
    except KeyboardInterrupt:
        code = 130
    finally:
        try:
            write_record(args.record,
                         build_record(getattr(runner, "last_result", None),
                                      args.modules, started_at,
                                      time.monotonic() - started, complete))
        except Exception as exc:
            print(f"timed_unittest: timing record not written: "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
