"""Exercise the deterministic shard assignment and CLI plumbing of
tests/run_test_shards.py without running the suite: the module list comes from
the same glob the runner uses, weights come from tests/shard_weights.json, and
subprocess.run is replaced by a recorder.
"""
import contextlib
from collections import Counter
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
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

    def _weights_doc(self, weights, samples=None):
        return {"_meta": {"format": "project_room_shard_weights_v2"},
                "weights": dict(weights),
                "samples": (dict(samples) if samples is not None
                            else {module: 1 for module in weights})}

    def _combined(self, run_id, revision, jobs):
        return {"format": "project_room_timing_combined_v1",
                "run_id": run_id, "revision": revision, "jobs": jobs}

    def _job(self, os_name, python, modules, complete=True):
        return {"os": os_name, "python": python,
                "record": {"complete": complete, "modules": modules,
                           "summary": {}, "shard": {"index": 0, "count": 5}}}

    def test_skewed_weights_isolate_the_heavy_module(self):
        weights = {"test_a": 1000, "test_b": 1, "test_c": 1, "test_d": 1}
        groups, loads = run_test_shards.assign(
            ["test_a", "test_b", "test_c", "test_d"], 2, weights)
        self.assertEqual(groups, [["test_a"], ["test_b", "test_c", "test_d"]])

    def test_an_unweighted_module_fails_assignment_with_the_measure_command(self):
        weights = {"test_a": 10, "test_b": 20}
        with self.assertRaises(run_test_shards.MissingWeights) as ctx:
            run_test_shards.assign(["test_a", "test_b", "test_zz"], 2, weights)
        self.assertEqual(ctx.exception.missing, ["test_zz"])
        self.assertEqual(ctx.exception.orphaned, [])

    def test_shard_mode_reports_the_measure_command_for_missing_weights(self):
        weights = run_test_shards.load_weights()
        dropped = sorted(weights)[0]
        del weights[dropped]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shard_weights.json"
            path.write_text(json.dumps(self._weights_doc(weights)))
            err = io.StringIO()
            with mock.patch.object(run_test_shards, "WEIGHTS_FILE", path), \
                    contextlib.redirect_stderr(err):
                code = run_test_shards.main(["--shard", "0", "--shards", "5"])
        self.assertEqual(code, 2)
        self.assertIn(f"--measure {dropped}", err.getvalue())

    def test_every_discovered_module_has_a_pinned_weight(self):
        weights = run_test_shards.load_weights()
        modules = run_test_shards.test_modules()
        missing = sorted(set(modules) - set(weights))
        orphaned = sorted(set(weights) - set(modules))
        self.assertEqual((missing, orphaned), ([], []),
                         "run: python3 tests/run_test_shards.py --measure "
                         + " ".join(missing)
                         + (" (drop stale entries with --refresh-weights: "
                            + ", ".join(orphaned) + ")" if orphaned else ""))

    def test_check_weights_reports_coverage_and_local_estimates(self):
        weights = run_test_shards.load_weights()
        samples = {module: 2 for module in weights}
        samples["test_docs_links"] = 0
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "w.json"
            path.write_text(json.dumps(self._weights_doc(weights, samples)))
            out = io.StringIO()
            with mock.patch.object(run_test_shards, "WEIGHTS_FILE", path), \
                    contextlib.redirect_stdout(out):
                code = run_test_shards.main(["--check-weights"])
        self.assertEqual(code, 0)
        self.assertIn(f"weights: {len(weights)} modules covered, "
                      f"1 local estimates (samples 0)", out.getvalue())

    def test_check_weights_fails_for_missing_and_orphaned_entries(self):
        weights = run_test_shards.load_weights()
        dropped = sorted(weights)[0]
        del weights[dropped]
        weights["test_not_a_module"] = 5.0
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "w.json"
            path.write_text(json.dumps(self._weights_doc(weights)))
            err = io.StringIO()
            with mock.patch.object(run_test_shards, "WEIGHTS_FILE", path), \
                    contextlib.redirect_stderr(err):
                code = run_test_shards.main(["--check-weights"])
        self.assertEqual(code, 2)
        text = err.getvalue()
        self.assertIn(dropped, text)
        self.assertIn("test_not_a_module", text)
        self.assertIn("--check-weights", text)

    def test_refresh_weights_takes_the_median_of_qualifying_jobs(self):
        weights = run_test_shards.load_weights()
        target, other, keep = ("test_docs_links", "test_run_test_shards",
                               "test_ci_timing_summary")
        weights["test_orphan_zzz"] = 9.0  # stale entry, must be dropped
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            file1 = tmp / "c1.json"
            file1.write_text(json.dumps(self._combined("111", "abcdef1234567", [
                self._job("macos", "3.11", {target: {"wall_seconds": 10}}),
                self._job("macos", "3.11", {target: {"wall_seconds": 20}}),
                self._job("linux", "3.11", {target: {"wall_seconds": 999}}),
                self._job("macos", "3.12", {target: {"wall_seconds": 999}}),
                self._job("macos", "3.11", {target: {"wall_seconds": 999}},
                          complete=False)])))
            file2 = tmp / "c2.json"
            file2.write_text(json.dumps(self._combined("222", "1234567abcdef", [
                self._job("macos", "3.11", {target: {"wall_seconds": 30},
                                            other: {"wall_seconds": 42}})])))
            big = tmp / "big.json"
            big.write_text(" " * (9 * 1024 * 1024))
            wrong = tmp / "wrong.json"
            wrong.write_text(json.dumps({"format": "other"}))
            wpath = tmp / "w.json"
            wpath.write_text(json.dumps(self._weights_doc(weights)))
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(run_test_shards, "WEIGHTS_FILE", wpath), \
                    contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                code = run_test_shards.main(
                    ["--refresh-weights", str(file1), str(file2),
                     str(big), str(wrong)])
            self.assertEqual(code, 0)
            data = json.loads(wpath.read_text())
        self.assertEqual(data["weights"][target], 20.0)  # median of 10, 20, 30
        self.assertEqual(data["samples"][target], 3)
        self.assertEqual(data["weights"][other], 42.0)
        self.assertEqual(data["samples"][other], 1)
        self.assertEqual(data["weights"][keep], weights[keep])  # kept, untouched
        self.assertNotIn("test_orphan_zzz", data["weights"])
        self.assertIn(f"ignored {big}", err.getvalue())
        self.assertIn(f"ignored {wrong}", err.getvalue())
        self.assertIn("run 111 (abcdef1, macos-latest/3.11, 2 jobs)",
                      data["_meta"]["sources"])
        self.assertIn("run 222 (1234567, macos-latest/3.11, 1 jobs)",
                      data["_meta"]["sources"])
        self.assertIn("refreshed_at", data["_meta"])
        text = out.getvalue()
        self.assertIn("test_docs_links", text)
        self.assertIn("kept previous values", text)
        self.assertIn("dropped stale entries", text)

    def test_refresh_weights_fails_when_a_module_has_neither_weight_nor_sample(self):
        weights = run_test_shards.load_weights()
        dropped = sorted(weights)[0]
        del weights[dropped]
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            wpath = tmp / "w.json"
            before = json.dumps(self._weights_doc(weights), indent=1,
                                sort_keys=True)
            wpath.write_text(before)
            combined = tmp / "c.json"
            combined.write_text(json.dumps(self._combined("1", "abc", [])))
            err = io.StringIO()
            with mock.patch.object(run_test_shards, "WEIGHTS_FILE", wpath), \
                    contextlib.redirect_stderr(err):
                code = run_test_shards.main(["--refresh-weights", str(combined)])
            self.assertEqual(code, 2)
            self.assertEqual(wpath.read_text(), before)
        self.assertIn(f"--measure {dropped}", err.getvalue())

    def test_measure_pins_a_local_estimate(self):
        weights = run_test_shards.load_weights()
        target = "test_docs_links"

        def fake_run(argv, **kwargs):
            record = Path(argv[argv.index("--record") + 1])
            record.write_text(json.dumps(
                {"complete": True,
                 "modules": {target: {"wall_seconds": 12.5}}}))
            return mock.Mock(returncode=0)

        with tempfile.TemporaryDirectory() as tmp:
            wpath = Path(tmp) / "w.json"
            wpath.write_text(json.dumps(self._weights_doc(weights)))
            out = io.StringIO()
            with mock.patch.object(run_test_shards, "WEIGHTS_FILE", wpath), \
                    mock.patch.object(run_test_shards.subprocess, "run",
                                      side_effect=fake_run), \
                    contextlib.redirect_stdout(out):
                code = run_test_shards.main(["--measure", target])
            self.assertEqual(code, 0)
            data = json.loads(wpath.read_text())
        self.assertEqual(data["weights"][target], 12.5)
        self.assertEqual(data["samples"][target], 0)
        self.assertIn(f"{target}: 12.5 s (local estimate, samples 0)",
                      out.getvalue())

    def test_measure_does_not_write_on_an_incomplete_record(self):
        weights = run_test_shards.load_weights()

        def fake_run(argv, **kwargs):
            record = Path(argv[argv.index("--record") + 1])
            record.write_text(json.dumps({"complete": False, "modules": {}}))
            return mock.Mock(returncode=0)

        with tempfile.TemporaryDirectory() as tmp:
            wpath = Path(tmp) / "w.json"
            before = json.dumps(self._weights_doc(weights), indent=1,
                                sort_keys=True)
            wpath.write_text(before)
            err = io.StringIO()
            with mock.patch.object(run_test_shards, "WEIGHTS_FILE", wpath), \
                    mock.patch.object(run_test_shards.subprocess, "run",
                                      side_effect=fake_run), \
                    contextlib.redirect_stderr(err):
                code = run_test_shards.main(["--measure", "test_docs_links"])
            self.assertEqual(code, 1)
            self.assertEqual(wpath.read_text(), before)

    def test_stale_estimates_needs_both_factor_and_absolute_margin(self):
        weights = {"a": 100.0, "b": 100.0, "c": 100.0, "d": 10.0, "e": 1000.0}
        measured = {"a": 140.0,  # 40 s off but only 1.4x
                    "b": 220.0,  # 2.2x and 120 s: stale
                    "c": 60.0,   # 40 s off but only 1.7x
                    "d": 25.0,   # 2.5x but only 15 s
                    "e": 500.0}  # exactly 2x, not more
        self.assertEqual(run_test_shards.stale_estimates(weights, measured),
                         ["b"])

    def test_shard_inventory_equals_the_discovered_test_ids(self):
        discovered = unittest.TestLoader().discover(
            str(ROOT), pattern="test*.py", top_level_dir=str(ROOT))

        def ids(suite):
            found = []
            stack = [suite]
            while stack:
                item = stack.pop()
                if isinstance(item, unittest.TestSuite):
                    stack.extend(item)
                else:
                    found.append(item.id())
            return found

        modules = run_test_shards.test_modules()
        groups, _ = run_test_shards.assign(
            modules, 5, run_test_shards.load_weights())
        loader = unittest.TestLoader()
        union = []
        for group in groups:
            union.extend(ids(loader.loadTestsFromNames(group)))
        self.assertEqual(Counter(union), Counter(ids(discovered)))

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
        self.assertIsNone(runner.call_args.kwargs["env"])

    def test_shard_mode_with_record_invokes_the_timed_runner(self):
        modules = run_test_shards.test_modules()
        groups, _ = run_test_shards.assign(modules, 5, run_test_shards.load_weights())
        runner = mock.Mock(return_value=mock.Mock(returncode=0))
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(run_test_shards.subprocess, "run", runner):
                code = run_test_shards.main(
                    ["--shard", "1", "--shards", "5", "--record", tmp])
            self.assertEqual(code, 0)
        argv = runner.call_args.args[0]
        record = str(Path(tmp) / "shard-1-of-5.json")
        self.assertEqual(argv[:5], [sys.executable, "-B",
                                    str(ROOT / "tests" / "timed_unittest.py"),
                                    "--record", record])
        self.assertEqual(argv[5:], groups[1])
        env = runner.call_args.kwargs["env"]
        self.assertEqual(env["PROJECT_ROOM_TIMING_SHARD"], "1/5")
        for key in ("PATH", "HOME"):
            if key in os.environ:
                self.assertEqual(env[key], os.environ[key])

    def test_jobs_mode_with_record_gives_each_shard_a_record_and_env(self):
        def fake_popen(argv, **kwargs):
            kwargs["stdout"].write(
                "x\n----------------------------------------------------------------------\n"
                "Ran 1 tests in 0.001s\n\nOK\n")
            return mock.Mock(wait=lambda: 0)

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(run_test_shards.subprocess, "Popen",
                                   side_effect=fake_popen) as popen, \
                    contextlib.redirect_stdout(io.StringIO()):
                code = run_test_shards.main(
                    ["--jobs", "5", "--record", tmp, "--log-dir", tmp])
        self.assertEqual(code, 0)
        self.assertEqual(len(popen.call_args_list), 5)
        seen = set()
        for call in popen.call_args_list:
            argv = call.args[0]
            record = argv[argv.index("--record") + 1]
            seen.add((Path(record).name,
                      call.kwargs["env"]["PROJECT_ROOM_TIMING_SHARD"]))
        self.assertEqual(seen,
                         {(f"shard-{i}-of-5.json", f"{i}/5") for i in range(5)})

    def test_unittest_cmd_forms(self):
        self.assertEqual(
            run_test_shards.unittest_cmd(["test_a", "test_b"], False),
            [sys.executable, "-B", "-m", "unittest", "test_a", "test_b"])
        self.assertEqual(
            run_test_shards.unittest_cmd(["test_a"], True),
            [sys.executable, "-B", "-m", "unittest", "-v", "test_a"])
        record = Path("dir") / "shard-0-of-5.json"
        self.assertEqual(
            run_test_shards.unittest_cmd(["test_a"], True, record),
            [sys.executable, "-B", str(ROOT / "tests" / "timed_unittest.py"),
             "--record", str(record), "-v", "test_a"])

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
