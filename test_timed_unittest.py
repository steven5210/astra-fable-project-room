"""Exercise tests/timed_unittest.py in-process: the runner is imported from its
file path (the same way test_run_test_shards imports the shard runner) and
driven against synthetic modules written to a temporary directory on sys.path,
so no real suite module is needed and nothing leaves the process.
"""
import contextlib
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "timed_unittest", ROOT / "tests" / "timed_unittest.py")
timed_unittest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(timed_unittest)

MOD_OUTCOMES = """\
import time
import unittest


class TimedSyntheticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        time.sleep(0.05)

    def setUp(self):
        time.sleep(0.05)

    def tearDown(self):
        time.sleep(0.05)

    def test_ok(self):
        time.sleep(0.05)

    def test_fail(self):
        self.fail("synthetic failure")

    def test_error(self):
        raise RuntimeError("synthetic error")

    @unittest.skip("synthetic skip")
    def test_skip(self):
        pass

    @unittest.expectedFailure
    def test_expected_failure(self):
        self.fail("synthetic expected failure")

    @unittest.expectedFailure
    def test_unexpected_success(self):
        pass

    def test_subtest_fail(self):
        with self.subTest(i=0):
            pass
        with self.subTest(i=1):
            self.fail("synthetic subtest failure")


class SecondTimedTests(unittest.TestCase):
    # The first class's setUpClass runs before the first startTest and is
    # outside the module wall; a later class's lands in the wall gap.
    @classmethod
    def setUpClass(cls):
        time.sleep(0.05)

    def test_second(self):
        pass
"""

MOD_PASS = """\
import unittest


class PassingTests(unittest.TestCase):
    def test_one(self):
        pass

    def test_two(self):
        pass
"""

MOD_INTERRUPT = """\
import unittest


class InterruptTests(unittest.TestCase):
    def test_first(self):
        pass

    def test_interrupt(self):
        raise KeyboardInterrupt
"""

MOD_NESTED = """\
import io
import unittest


class NestedRunTests(unittest.TestCase):
    def test_inner_runner(self):
        import timed_unittest

        class Inner(unittest.TestCase):
            def test_inner_one(self):
                pass

            def test_inner_two(self):
                pass

        runner = timed_unittest.TimedTextTestRunner(stream=io.StringIO())
        runner.run(unittest.TestLoader().loadTestsFromTestCase(Inner))
"""

MOD_DUP_A = """\
import time
import unittest

counter = {"runs": 0}


class DupCase(unittest.TestCase):
    def setUp(self):
        # The first run sleeps 0.5 s, the second 0.05: a row that accumulated
        # phase time instead of measuring its own run would read ~0.55.
        time.sleep(0.5 if counter["runs"] == 0 else 0.05)

    def test_dup(self):
        counter["runs"] += 1
        if counter["runs"] == 1:
            self.fail("synthetic first-run failure")
"""

MOD_DUP_B = """\
from timed_synth_dup_a import DupCase
"""


def suite_ids(suite):
    ids = []
    stack = [suite]
    while stack:
        item = stack.pop()
        if isinstance(item, unittest.TestSuite):
            stack.extend(item)
        else:
            ids.append(item.id())
    return sorted(ids)


class TimedUnittestTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp = Path(self._tmpdir.name)
        sys.path.insert(0, str(self.tmp))
        self.addCleanup(sys.path.remove, str(self.tmp))
        # The synthetic module under test imports timed_unittest by name.
        sys.modules["timed_unittest"] = timed_unittest
        self.addCleanup(lambda: sys.modules.pop("timed_unittest", None))

    def _write_module(self, name, source):
        (self.tmp / f"{name}.py").write_text(source)
        importlib.invalidate_caches()
        sys.modules.pop(name, None)

    def _run(self, modules, verbose=False, record_name="record.json"):
        record = self.tmp / record_name
        argv = ["--record", str(record)] + (["-v"] if verbose else []) + list(modules)
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            code = timed_unittest.main(argv)
        return code, record, output.getvalue()

    def _load(self, record):
        return json.loads(record.read_text())

    def test_outcomes_phases_and_module_aggregates(self):
        self._write_module("timed_synth_outcomes", MOD_OUTCOMES)
        code, record, output = self._run(["timed_synth_outcomes"], verbose=True)
        self.assertEqual(code, 1)
        data = self._load(record)
        self.assertEqual(data["format"], "project_room_test_timing_v1")
        self.assertTrue(data["complete"])
        summary = data["summary"]
        self.assertEqual(
            (summary["ran"], summary["failures"], summary["errors"],
             summary["skipped"], summary["expected_failures"],
             summary["unexpected_successes"], summary["was_successful"]),
            (8, 2, 1, 1, 1, 1, False))
        rows = {row["id"].rsplit(".", 1)[-1]: row for row in data["tests"]}
        self.assertEqual(len(rows), 8)
        self.assertEqual({name: row["outcome"] for name, row in rows.items()}, {
            "test_ok": "ok", "test_fail": "fail", "test_error": "error",
            "test_skip": "skip", "test_expected_failure": "expected_failure",
            "test_unexpected_success": "unexpected_success",
            "test_subtest_fail": "fail", "test_second": "ok"})
        ok = rows["test_ok"]
        self.assertGreaterEqual(ok["setup_seconds"], 0.04)
        self.assertGreaterEqual(ok["body_seconds"], 0.04)
        self.assertGreaterEqual(ok["teardown_seconds"], 0.04)
        # Each field is rounded to 3 decimals; the rounded phase sum can exceed
        # the rounded total by up to ~1.5 ms.
        self.assertGreaterEqual(
            ok["seconds"],
            ok["setup_seconds"] + ok["body_seconds"] + ok["teardown_seconds"]
            - 0.002)
        module = data["modules"]["timed_synth_outcomes"]
        self.assertEqual(module["tests"], 8)
        self.assertEqual(module["outcomes"], {
            "ok": 2, "fail": 2, "error": 1, "skip": 1,
            "expected_failure": 1, "unexpected_success": 1})
        self.assertGreaterEqual(module["fixture_gap_seconds"], 0.04)
        self.assertGreaterEqual(module["wall_seconds"], module["test_seconds"])
        self.assertIn("Ran 8 tests in", output)
        self.assertIn("FAILED (", output)
        self.assertIn("failures=2", output)
        self.assertIn("errors=1", output)
        self.assertIn("skipped=1", output)

    def test_a_passing_run_exits_zero_and_marks_complete(self):
        self._write_module("timed_synth_pass", MOD_PASS)
        code, record, output = self._run(["timed_synth_pass"], verbose=True)
        self.assertEqual(code, 0)
        data = self._load(record)
        self.assertTrue(data["complete"])
        self.assertTrue(data["summary"]["was_successful"])
        self.assertEqual(data["summary"]["ran"], 2)
        self.assertIn("Ran 2 tests in", output)
        self.assertIn("... ok", output)
        self.assertTrue(output.rstrip().endswith("OK"))

    def test_a_keyboard_interrupt_writes_a_partial_record_and_exits_130(self):
        self._write_module("timed_synth_interrupt", MOD_INTERRUPT)
        code, record, _ = self._run(["timed_synth_interrupt"])
        self.assertEqual(code, 130)
        data = self._load(record)
        self.assertFalse(data["complete"])
        self.assertEqual(data["summary"]["ran"], 2)
        self.assertEqual({row["id"].rsplit(".", 1)[-1] for row in data["tests"]},
                         {"test_first", "test_interrupt"})

    def test_a_nested_inprocess_run_records_only_its_own_ids(self):
        self._write_module("timed_synth_nested", MOD_NESTED)
        code, record, _ = self._run(["timed_synth_nested"])
        self.assertEqual(code, 0)
        data = self._load(record)
        self.assertEqual(data["summary"]["ran"], 1)
        self.assertEqual(len(data["tests"]), 1)
        row = data["tests"][0]
        self.assertTrue(row["id"].endswith("NestedRunTests.test_inner_runner"))
        self.assertNotIn("Inner", row["id"])

    def test_loaded_ids_equal_the_standard_loader(self):
        names = ["test_run_test_shards", "test_docs_links"]
        self.assertEqual(suite_ids(timed_unittest.load_suite(names)),
                         suite_ids(unittest.TestLoader().loadTestsFromNames(names)))

    def test_phase_hooks_and_the_active_stack_are_restored(self):
        self._write_module("timed_synth_pass", MOD_PASS)
        originals = {name: getattr(unittest.TestCase, name)
                     for name in timed_unittest._PHASE_HOOKS}
        depth = len(getattr(unittest.TestCase,
                           "_project_room_timing_stack", []))
        code, _, _ = self._run(["timed_synth_pass"])
        self.assertEqual(code, 0)
        for name, original in originals.items():
            self.assertIs(getattr(unittest.TestCase, name), original, name)
        self.assertEqual(len(getattr(unittest.TestCase,
                                     "_project_room_timing_stack", [])), depth)

    def test_the_row_cap_marks_truncation_but_counts_everything(self):
        self._write_module("timed_synth_outcomes", MOD_OUTCOMES)
        with mock.patch.object(timed_unittest, "MAX_TEST_ROWS", 3):
            code, record, _ = self._run(["timed_synth_outcomes"])
        self.assertEqual(code, 1)
        data = self._load(record)
        self.assertTrue(data["tests_truncated"])
        self.assertEqual(len(data["tests"]), 3)
        self.assertEqual(data["summary"]["ran"], 8)
        self.assertEqual(data["modules"]["timed_synth_outcomes"]["tests"], 8)

    def test_a_duplicated_test_id_gets_two_independent_rows(self):
        self._write_module("timed_synth_dup_a", MOD_DUP_A)
        self._write_module("timed_synth_dup_b", MOD_DUP_B)
        code, record, _ = self._run(["timed_synth_dup_a", "timed_synth_dup_b"])
        self.assertEqual(code, 1)
        rows = [row for row in self._load(record)["tests"]
                if row["id"] == "timed_synth_dup_a.DupCase.test_dup"]
        self.assertEqual(len(rows), 2)
        self.assertEqual([row["outcome"] for row in rows], ["fail", "ok"])
        first, second = rows
        self.assertGreaterEqual(first["setup_seconds"], 0.45)
        # 0.25 s of noise budget on a 0.05 s sleep; the cumulative-rows bug
        # would read >= 0.55 here.
        self.assertLess(second["setup_seconds"], 0.3)

    def test_an_import_failure_is_attributed_to_the_requested_module(self):
        # loadTestsFromNames turns an ImportError into a _FailedTest entry.
        self._write_module("timed_synth_broken",
                           "import a_module_that_does_not_exist_xyz\n")
        code, record, _ = self._run(["timed_synth_broken"])
        self.assertEqual(code, 1)
        data = self._load(record)
        self.assertIn("timed_synth_broken", data["modules"])
        self.assertEqual(data["modules"]["timed_synth_broken"]["outcomes"],
                         {"error": 1})
        self.assertEqual(data["tests"][0]["module"], "timed_synth_broken")
        self.assertEqual(data["tests"][0]["outcome"], "error")

    def test_sys_path_parity_with_unittest_dash_m(self):
        out_file = self.tmp / "sys_path.json"
        self._write_module("timed_synth_probe", (
            "import json, sys, unittest\n"
            "from pathlib import Path\n"
            f"Path({str(out_file)!r}).write_text(json.dumps(sys.path))\n"
            "\n"
            "class ProbeTests(unittest.TestCase):\n"
            "    def test_x(self):\n"
            "        pass\n"))
        record = self.tmp / "probe.json"
        pythonpath = str(self.tmp)
        if os.environ.get("PYTHONPATH"):
            pythonpath += os.pathsep + os.environ["PYTHONPATH"]
        env = dict(os.environ, PYTHONPATH=pythonpath)
        proc = subprocess.run(
            [sys.executable, "-B", str(ROOT / "tests" / "timed_unittest.py"),
             "--record", str(record), "timed_synth_probe"],
            cwd=ROOT, env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        paths = [Path(entry).resolve()
                 for entry in json.loads(out_file.read_text()) if entry]
        self.assertEqual(paths[0], ROOT)
        self.assertNotIn((ROOT / "tests").resolve(), paths)

    def test_record_provenance_comes_from_the_environment(self):
        self._write_module("timed_synth_pass", MOD_PASS)
        env = {"PROJECT_ROOM_TIMING_SHARD": "2/5",
               "PROJECT_ROOM_TIMING_REVISION": "abc1234",
               "PROJECT_ROOM_TIMING_RUN_ID": "4242"}
        with mock.patch.dict(os.environ, env):
            code, record, _ = self._run(["timed_synth_pass"])
        self.assertEqual(code, 0)
        data = self._load(record)
        self.assertEqual(data["shard"], {"index": 2, "count": 5})
        self.assertEqual(data["revision"], "abc1234")
        self.assertEqual(data["run_id"], "4242")
        self.assertEqual(data["modules_requested"], ["timed_synth_pass"])

    def _worker_record(self, module, ran, failures=0, rows=(), complete=True):
        return {"format": timed_unittest.FORMAT, "complete": complete,
                "python": "3.11.9", "python_minor": "3.11",
                "implementation": "CPython", "os": "macos",
                "platform": "worker-platform", "machine": "arm64",
                "revision": "rev1", "run_id": "run1",
                "started_at": "2026-10-18T00:00:00+00:00",
                "shard": {"index": 4, "count": 5},
                "summary": {"ran": ran, "failures": failures, "errors": 0,
                            "skipped": 0, "expected_failures": 0,
                            "unexpected_successes": 0,
                            "was_successful": failures == 0},
                "modules": {module: {"tests": ran, "wall_seconds": 1.0}},
                "tests": list(rows)}

    def test_merge_records_unions_modules_sums_counts_and_flags_incomplete(self):
        row_a = {"id": "a.C.t1", "module": "a", "seconds": 0.1}
        row_b = {"id": "b.C.t1", "module": "b", "seconds": 0.2}
        records = [self._worker_record("a", 1, rows=[row_a]),
                   self._worker_record("b", 2, failures=1, rows=[row_b]),
                   None]
        merged = timed_unittest.merge_records(
            records, wall_seconds=12.34567, modules_requested=["a", "b", "c"],
            workers=3,
            worker_runs=[{"module": "a", "exit_code": 0, "seconds": 1.0,
                          "record": True},
                         {"module": "b", "exit_code": 1, "seconds": 2.0,
                          "record": True},
                         {"module": "c", "exit_code": 1, "seconds": 0.5,
                          "record": False}])
        self.assertEqual(merged["format"], timed_unittest.FORMAT)
        self.assertFalse(merged["complete"])          # one worker record missing
        self.assertEqual(merged["summary"],
                         {"ran": 3, "failures": 1, "errors": 0, "skipped": 0,
                          "expected_failures": 0, "unexpected_successes": 0,
                          "was_successful": False})
        self.assertEqual(set(merged["modules"]), {"a", "b"})
        self.assertEqual([row["id"] for row in merged["tests"]],
                         ["a.C.t1", "b.C.t1"])        # queue order
        self.assertEqual(merged["wall_seconds"], 12.346)
        self.assertEqual(merged["modules_requested"], ["a", "b", "c"])
        self.assertEqual(merged["workers"], 3)
        self.assertEqual(merged["worker_runs"][2]["record"], False)
        self.assertEqual(merged["python"], "3.11.9")  # first worker's provenance
        self.assertEqual(merged["platform"], "worker-platform")
        self.assertEqual(merged["shard"], {"index": 4, "count": 5})
        self.assertNotIn("tests_truncated", merged)

    def test_merge_records_stamps_process_seconds_for_single_module_workers(self):
        records = [self._worker_record("a", 1), self._worker_record("b", 2)]
        merged = timed_unittest.merge_records(
            records, wall_seconds=1.0, modules_requested=["a", "b"],
            workers=2, worker_runs=[
                {"module": "a", "exit_code": 0, "seconds": 7.5,
                 "record": True},
                {"module": "b", "exit_code": 0, "seconds": 9.25,
                 "record": True}])
        self.assertEqual(merged["modules"]["a"]["process_seconds"], 7.5)
        self.assertEqual(merged["modules"]["b"]["process_seconds"], 9.25)
        # A worker record covering other modules besides its own stamps
        # nothing -- the process seconds cannot be attributed to one module.
        shared = self._worker_record("a", 1)
        shared["modules"]["b"] = {"tests": 2, "wall_seconds": 0.5}
        merged = timed_unittest.merge_records(
            [shared], wall_seconds=1.0, modules_requested=["a"],
            workers=2, worker_runs=[
                {"module": "a", "exit_code": 0, "seconds": 7.5,
                 "record": True}])
        self.assertNotIn("process_seconds", merged["modules"]["a"])

    def test_merge_records_marks_truncation_and_survives_zero_records(self):
        rows = [{"id": f"m.C.t{i}", "module": "m"} for i in range(3)]
        one = self._worker_record("m", 3, rows=rows)
        with mock.patch.object(timed_unittest, "MAX_TEST_ROWS", 2):
            merged = timed_unittest.merge_records(
                [one, one], wall_seconds=1.0, modules_requested=["m", "n"],
                workers=2, worker_runs=[])
        self.assertTrue(merged["tests_truncated"])
        self.assertEqual(len(merged["tests"]), 2)
        self.assertEqual(merged["summary"]["ran"], 6)
        empty = timed_unittest.merge_records(
            [None], wall_seconds=0.5, modules_requested=["x"], workers=2,
            worker_runs=[{"module": "x", "exit_code": 1, "seconds": 0.5,
                          "record": False}])
        self.assertFalse(empty["complete"])
        self.assertFalse(empty["summary"]["was_successful"])
        self.assertEqual(empty["summary"]["ran"], 0)
        self.assertEqual(empty["modules"], {})
        self.assertIn("python", empty)  # provenance filled like build_record


if __name__ == "__main__":
    unittest.main()
