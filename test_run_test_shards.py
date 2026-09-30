"""Exercise the deterministic shard assignment and CLI plumbing of
tests/run_test_shards.py without running the suite: the module list comes from
the same glob the runner uses, weights come from tests/shard_weights.json, and
subprocess.run is replaced by a recorder.
"""
import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "run_test_shards", ROOT / "tests" / "run_test_shards.py")
run_test_shards = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_test_shards)


class RunTestShardsTests(unittest.TestCase):
    def test_real_modules_partition_exactly_once_and_deterministically(self):
        modules = run_test_shards.test_modules()
        groups, loads = run_test_shards.assign(modules, 5, run_test_shards.load_weights())
        self.assertEqual(sorted(module for group in groups for module in group), modules)
        self.assertTrue(all(groups))
        again, again_loads = run_test_shards.assign(modules, 5, run_test_shards.load_weights())
        self.assertEqual((groups, loads), (again, again_loads))

    def test_skewed_weights_isolate_the_heavy_module(self):
        weights = {"test_a": 1000, "test_b": 1, "test_c": 1, "test_d": 1}
        groups, loads = run_test_shards.assign(
            ["test_a", "test_b", "test_c", "test_d"], 2, weights)
        self.assertEqual(groups, [["test_a"], ["test_b", "test_c", "test_d"]])

    def test_unknown_module_weighs_the_median(self):
        weights = {"test_a": 10, "test_b": 30, "test_c": 50}
        groups, loads = run_test_shards.assign(
            ["test_a", "test_b", "test_c", "test_zz"], 4, weights)
        index = next(i for i, group in enumerate(groups) if "test_zz" in group)
        self.assertEqual(loads[index], 30)

    def test_missing_weights_file_uses_equal_weights(self):
        groups, loads = run_test_shards.assign(
            ["test_a", "test_b", "test_c", "test_d"], 2, {})
        self.assertEqual(loads, [2.0, 2.0])

    def test_shard_mode_invokes_unittest_with_the_assigned_modules(self):
        modules = run_test_shards.test_modules()
        groups, _ = run_test_shards.assign(modules, 5, run_test_shards.load_weights())
        runner = mock.Mock(return_value=mock.Mock(returncode=0))
        with mock.patch.object(run_test_shards.subprocess, "run", runner):
            code = run_test_shards.main(["--shard", "1", "--shards", "5"])
        self.assertEqual(code, 0)
        argv = runner.call_args.args[0]
        self.assertEqual(argv[-len(groups[1]):], groups[1])
        self.assertEqual(argv[:4], [sys.executable, "-B", "-m", "unittest"])

    def test_list_reports_each_shard_with_minutes_and_the_total(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = run_test_shards.main(["--list", "--shards", "5"])
        self.assertEqual(code, 0)
        lines = output.getvalue().splitlines()
        shard_lines = [line for line in lines if line.startswith("shard ") and "/5" in line]
        self.assertEqual(len(shard_lines), 5)
        self.assertTrue(all(" min):" in line for line in shard_lines))
        self.assertIn(f"total: {len(run_test_shards.test_modules())} modules", lines)


if __name__ == "__main__":
    unittest.main()
