"""Structured unittest runner for the workflow evaluation. Standard library only.

Invoked as ``python3 -B structured_unittest.py <test_file> <out_json>`` inside a disposable
candidate copy. It loads the test module the way ``python3 -B <test_file>`` would (its
directory on sys.path), runs the defaultTestLoader suite through TextTestRunner, and writes a
JSON record the probes classify without any console-text parsing: unittest's own
failures/errors/skipped split is the classification. Assertion failures are behavior reds;
errors are runtime errors; a load or runner exception is a harness error. The runner exits 0
whenever it wrote its record, so a module-level SystemExit or ImportError cannot masquerade
as test results.
"""
import hashlib
import importlib.util
import io
import json
import pathlib
import sys
import traceback
import unittest

MAX_ASSERTION = 200
MAX_HARNESS = 2000


def _last_line(exc_text, limit):
    lines = [line for line in exc_text.strip().splitlines() if line.strip()]
    return (lines[-1] if lines else exc_text.strip())[:limit]


def main(argv):
    if len(argv) != 2:
        sys.stderr.write("usage: structured_unittest.py TEST_FILE OUT_JSON\n")
        return 2
    test_file = pathlib.Path(argv[0])
    out_json = pathlib.Path(argv[1])
    stream = io.StringIO()
    record = {"ran": 0, "failures": [], "errors": [], "skipped": 0,
              "expected_failures": 0, "unexpected_successes": 0, "harness_error": None}
    runner = unittest.TextTestRunner(stream=stream, verbosity=2)
    try:
        sys.path.insert(0, str(test_file.resolve().parent))
        spec = importlib.util.spec_from_file_location("candidate_tests", test_file)
        module = importlib.util.module_from_spec(spec)
        sys.modules["candidate_tests"] = module
        spec.loader.exec_module(module)
        suite = unittest.defaultTestLoader.loadTestsFromModule(module)
        result = runner.run(suite)
        record["ran"] = result.testsRun
        record["failures"] = [{"test": str(test), "assertion": _last_line(text, MAX_ASSERTION)}
                              for test, text in result.failures]
        record["errors"] = [{"test": str(test), "exception": _last_line(text, MAX_ASSERTION)}
                            for test, text in result.errors]
        record["skipped"] = len(result.skipped)
        record["expected_failures"] = len(result.expectedFailures)
        record["unexpected_successes"] = len(result.unexpectedSuccesses)
    except BaseException:
        record["harness_error"] = traceback.format_exc()[-MAX_HARNESS:]
    output = stream.getvalue()
    record["runner_output_sha256"] = hashlib.sha256(output.encode()).hexdigest()
    record["runner_output_bytes"] = len(output.encode())
    out_json.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
