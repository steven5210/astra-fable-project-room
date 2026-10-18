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
        time.sleep(0.05)

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
        self.assertGreaterEqual(
            ok["seconds"],
            ok["setup_seconds"] + ok["body_seconds"] + ok["teardown_seconds"])
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
        code, _, _ = self._run(["timed_synth_pass"])
        self.assertEqual(code, 0)
        for name, original in originals.items():
            self.assertIs(getattr(unittest.TestCase, name), original, name)
        self.assertEqual(timed_unittest._ACTIVE_PHASES, [])

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
        for row in rows:
            self.assertGreaterEqual(row["setup_seconds"], 0.04)
            self.assertLessEqual(row["setup_seconds"], 0.09)

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


if __name__ == "__main__":
    unittest.main()
