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


class _WorkerScript:
    """A controllable Popen factory: each child writes its scripted log body to
    the stdout handle at launch and finishes after a scripted number of polls,
    writing its scripted timing record (if any) at finish."""

    def __init__(self, logs=None, records=None, exits=None, deadlines=None):
        self.logs = logs or {}
        self.records = records or {}
        self.exits = exits or {}
        self.deadlines = deadlines or {}
        self.launched = []
        self.alive = 0
        self.max_alive = 0

    def popen(self, argv, **kwargs):
        return _WorkerProc(self, argv, kwargs)


class _WorkerProc:
    def __init__(self, script, argv, kwargs):
        self.script = script
        self.argv = argv
        self.kwargs = kwargs
        self.module = argv[-1]
        self.returncode = None
        self.polls = 0
        # Modules still running at launch (a proc keeps returncode None until
        # its scripted deadline poll).
        self.peers = {proc.module for proc in script.launched
                      if proc.returncode is None}
        script.launched.append(self)
        script.alive += 1
        script.max_alive = max(script.max_alive, script.alive)
        log = script.logs.get(self.module, "Ran 1 tests in 0.001s\nOK\n")
        kwargs["stdout"].write(log if isinstance(log, bytes) else log.encode())

    def poll(self):
        self.polls += 1
        if (self.returncode is None
                and self.polls >= self.script.deadlines.get(self.module, 1)):
            self.returncode = self.script.exits.get(self.module, 0)
            self.script.alive -= 1
            record = self.script.records.get(self.module)
            if record is not None and "--record" in self.argv:
                path = Path(self.argv[self.argv.index("--record") + 1])
                path.write_text(record)
        return self.returncode


class RunTestShardsTests(unittest.TestCase):
    def test_real_modules_partition_exactly_once_and_deterministically(self):
        modules = run_test_shards.test_modules()
        groups, loads, missing = run_test_shards.assign(modules, 5, run_test_shards.load_weights())
        self.assertEqual(missing, [])
        self.assertEqual(sorted(module for group in groups for module in group), modules)
        self.assertTrue(all(groups))
        again, again_loads, _m = run_test_shards.assign(modules, 5, run_test_shards.load_weights())
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
        groups, loads, _missing = run_test_shards.assign(
            ["test_a", "test_b", "test_c", "test_d"], 2, weights)
        self.assertEqual(groups, [["test_a"], ["test_b", "test_c", "test_d"]])

    def test_an_unweighted_module_is_scheduled_at_the_median(self):
        weights = {"test_a": 10, "test_b": 20}
        groups, loads, missing = run_test_shards.assign(
            ["test_a", "test_b", "test_zz"], 2, weights)
        self.assertEqual(missing, ["test_zz"])
        # median(10, 20) = 15 -> LPT: b(20) alone, then zz(15) and a(10).
        self.assertEqual(groups, [["test_b"], ["test_a", "test_zz"]])
        self.assertEqual(loads, [20, 25])

    def test_shard_mode_warns_and_runs_with_missing_weights(self):
        # The runner warns and estimates at the median; the hard failure is
        # the pin test and --check-weights.
        weights = run_test_shards.load_weights()
        dropped = sorted(weights)[0]
        del weights[dropped]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shard_weights.json"
            path.write_text(json.dumps(self._weights_doc(weights)))
            err = io.StringIO()
            with mock.patch.object(run_test_shards, "WEIGHTS_FILE", path), \
                    contextlib.redirect_stderr(err), \
                    mock.patch("subprocess.run") as run:
                run.return_value = mock.Mock(returncode=0)
                code = run_test_shards.main(["--shard", "0", "--shards", "5"])
        self.assertEqual(code, 0)
        self.assertIn("warning: discovered test modules without a pinned "
                      "weight (estimated at the median", err.getvalue())
        self.assertIn(dropped, err.getvalue())
        self.assertIn(f"--measure {dropped}", err.getvalue())
        run.assert_called_once()

    def test_list_mode_warns_and_continues_with_missing_weights(self):
        weights = run_test_shards.load_weights()
        dropped = sorted(weights)[0]
        del weights[dropped]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shard_weights.json"
            path.write_text(json.dumps(self._weights_doc(weights)))
            err, out = io.StringIO(), io.StringIO()
            with mock.patch.object(run_test_shards, "WEIGHTS_FILE", path), \
                    contextlib.redirect_stderr(err), \
                    contextlib.redirect_stdout(out):
                code = run_test_shards.main(["--list", "--shards", "5"])
        self.assertEqual(code, 0)
        self.assertIn("warning: discovered test modules without a pinned "
                      "weight", err.getvalue())
        self.assertIn(f"--measure {dropped}", err.getvalue())
        self.assertIn("shard 0/5", out.getvalue())

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

    def test_refresh_weights_prefers_process_seconds(self):
        # The pool's whole-process wall (imports + first setUpClass included)
        # is what the weight should pin when the record carries it; wall and
        # process samples are never mixed within one module.
        weights = run_test_shards.load_weights()
        target = "test_docs_links"
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            file1 = tmp / "c1.json"
            file1.write_text(json.dumps(self._combined("111", "abcdef1", [
                self._job("macos", "3.11", {target: {"wall_seconds": 10,
                                                     "process_seconds": 40}}),
                self._job("macos", "3.11", {target: {"wall_seconds": 12}})])))
            wpath = tmp / "w.json"
            wpath.write_text(json.dumps(self._weights_doc(weights)))
            with mock.patch.object(run_test_shards, "WEIGHTS_FILE", wpath), \
                    contextlib.redirect_stdout(io.StringIO()):
                code = run_test_shards.main(
                    ["--refresh-weights", str(file1)])
            data = json.loads(wpath.read_text())
        self.assertEqual(code, 0)
        # One run has a process sample -> only process samples count.
        self.assertEqual(data["weights"][target], 40.0)
        self.assertEqual(data["samples"][target], 1)

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

    def test_measure_writes_once_after_every_module_succeeds(self):
        weights = run_test_shards.load_weights()
        targets = ["test_docs_links", "test_run_test_shards"]
        calls = {"n": 0}

        def fake_run(argv, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:  # the second module fails
                return mock.Mock(returncode=1)
            record = Path(argv[argv.index("--record") + 1])
            record.write_text(json.dumps(
                {"complete": True,
                 "modules": {targets[0]: {"wall_seconds": 10}}}))
            return mock.Mock(returncode=0)

        writes = []

        def counting_write(data):
            writes.append(dict(data["weights"]))

        with tempfile.TemporaryDirectory() as tmp:
            wpath = Path(tmp) / "w.json"
            before = json.dumps(self._weights_doc(weights), indent=1,
                                sort_keys=True)
            wpath.write_text(before)
            err = io.StringIO()
            with mock.patch.object(run_test_shards, "WEIGHTS_FILE", wpath), \
                    mock.patch.object(run_test_shards.subprocess, "run",
                                      side_effect=fake_run), \
                    mock.patch.object(run_test_shards, "write_weights_file",
                                      side_effect=counting_write), \
                    contextlib.redirect_stderr(err):
                code = run_test_shards.main(["--measure", *targets])
            self.assertEqual(code, 1)
            self.assertEqual(wpath.read_text(), before)  # nothing written
            self.assertEqual(writes, [])
            self.assertIn("weights file unchanged", err.getvalue())

            def fake_ok(argv, **kwargs):
                module = argv[-1]
                record = Path(argv[argv.index("--record") + 1])
                record.write_text(json.dumps(
                    {"complete": True,
                     "modules": {module: {"wall_seconds": 5}}}))
                return mock.Mock(returncode=0)

            out = io.StringIO()
            with mock.patch.object(run_test_shards, "WEIGHTS_FILE", wpath), \
                    mock.patch.object(run_test_shards.subprocess, "run",
                                      side_effect=fake_ok), \
                    mock.patch.object(run_test_shards, "write_weights_file",
                                      side_effect=counting_write), \
                    contextlib.redirect_stdout(out):
                code = run_test_shards.main(["--measure", *targets])
            self.assertEqual(code, 0)
            self.assertEqual(len(writes), 1)
            self.assertEqual({target: writes[0][target] for target in targets},
                             {"test_docs_links": 5.0,
                              "test_run_test_shards": 5.0})

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

        discovered_ids = ids(discovered)
        counts = Counter(discovered_ids)
        duplicates = sorted(name for name, count in counts.items() if count > 1)
        self.assertEqual(duplicates, [],
                         'discovery must collect each test id exactly once')
        executable_ids = [name for name in discovered_ids
                          if '.ExtensionExecutableEvolutionTests.' in name]
        self.assertEqual(len(executable_ids), 4)
        self.assertTrue(all(name.startswith('test_ao_review_extension.')
                            for name in executable_ids))
        modules = run_test_shards.test_modules()
        groups, _loads, _missing = run_test_shards.assign(
            modules, 5, run_test_shards.load_weights())
        loader = unittest.TestLoader()
        union = []
        for group in groups:
            union.extend(ids(loader.loadTestsFromNames(group)))
        self.assertEqual(Counter(union), counts)

    def test_shard_mode_invokes_unittest_with_the_assigned_modules(self):
        modules = run_test_shards.test_modules()
        groups, _loads, _missing = run_test_shards.assign(modules, 5, run_test_shards.load_weights())
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
        groups, _loads, _missing = run_test_shards.assign(modules, 5, run_test_shards.load_weights())
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

    def test_shard_workers_dispatch_longest_first_and_merge_the_records(self):
        group = ["wm", "wl", "wk", "wj", "wi"]
        weights = {"wm": 50, "wl": 40, "wk": 30, "wj": 20, "wi": 10}
        # wm holds a worker slot longest, so every later dispatch is observable.
        script = _WorkerScript(
            deadlines={"wm": 4},
            records={module: json.dumps(
                {"complete": True, "python": "fake", "os": "macos",
                 "summary": {"ran": 1, "was_successful": True},
                 "modules": {module: {"tests": 1}},
                 "tests": [{"id": f"{module}.C.t", "module": module}]})
                for module in group})
        with tempfile.TemporaryDirectory() as tmp:
            args = run_test_shards.parse_args(
                ["--shard", "0", "--workers", "2", "--record", tmp])
            out = io.StringIO()
            with mock.patch.object(run_test_shards.subprocess, "Popen",
                                   side_effect=script.popen), \
                    contextlib.redirect_stdout(out):
                code = run_test_shards.run_shard_workers(0, 5, group, weights,
                                                         args)
            merged = json.loads(
                (Path(tmp) / "shard-0-of-5.json").read_text())
        self.assertEqual(code, 0)
        self.assertEqual([proc.module for proc in script.launched],
                         ["wm", "wl", "wk", "wj", "wi"])  # longest first
        self.assertEqual(script.max_alive, 2)
        for proc in script.launched:
            record = (Path(tmp) / "workers" / "shard-0-of-5"
                      / f"{proc.module}.json")
            self.assertEqual(
                proc.argv,
                run_test_shards.unittest_cmd([proc.module], False, record))
            self.assertEqual(proc.kwargs["cwd"], run_test_shards.ROOT)
            self.assertEqual(
                proc.kwargs["env"]["PROJECT_ROOM_TIMING_SHARD"], "0/5")
        self.assertEqual(merged["workers"], 2)
        self.assertEqual([run["module"] for run in merged["worker_runs"]],
                         ["wm", "wl", "wk", "wj", "wi"])
        self.assertTrue(all(run["record"] for run in merged["worker_runs"]))
        self.assertTrue(merged["complete"])
        self.assertEqual(set(merged["modules"]), set(group))
        self.assertEqual(merged["summary"]["ran"], 5)
        self.assertEqual(merged["python"], "fake")
        text = out.getvalue()
        self.assertIn("total: 5 tests", text)
        self.assertIn("Ran 1 tests", text)
        self.assertLess(text.index("=== wl"), text.index("=== wm"))

    def test_exclusive_modules_run_alone_on_linux(self):
        group = ["wc", "test_verification", "wa", "test_verification_matrix",
                 "wb"]
        weights = {"test_verification": 5, "test_verification_matrix": 4,
                   "wa": 30, "wb": 20, "wc": 10}
        # wa holds its slot so the non-exclusive tail still overlaps.
        script = _WorkerScript(
            deadlines={"wa": 4},
            records={module: json.dumps(
                {"complete": True, "python": "fake", "os": "linux",
                 "summary": {"ran": 1, "was_successful": True},
                 "modules": {module: {"tests": 1}}, "tests": []})
                for module in group})
        with tempfile.TemporaryDirectory() as tmp:
            args = run_test_shards.parse_args(
                ["--shard", "0", "--workers", "3", "--record", tmp])
            with mock.patch.object(run_test_shards.subprocess, "Popen",
                                   side_effect=script.popen), \
                    mock.patch.object(run_test_shards.platform, "system",
                                      return_value="Linux"), \
                    contextlib.redirect_stdout(io.StringIO()):
                code = run_test_shards.run_shard_workers(
                    0, 5, group, weights, args)
            merged = json.loads(
                (Path(tmp) / "shard-0-of-5.json").read_text())
        self.assertEqual(code, 0)
        # Exclusive modules dispatch first (heavier one first), each with
        # zero overlap; nothing else sees them alive either.
        self.assertEqual([proc.module for proc in script.launched[:2]],
                         ["test_verification", "test_verification_matrix"])
        for proc in script.launched:
            if proc.module in run_test_shards.EXCLUSIVE_MODULES:
                self.assertEqual(proc.peers, set())
            else:
                self.assertFalse(
                    proc.peers & run_test_shards.EXCLUSIVE_MODULES)
        self.assertEqual(script.max_alive, 3)
        self.assertEqual(merged["exclusive"],
                         ["test_verification", "test_verification_matrix"])

    def test_exclusive_modules_interleave_normally_on_macos(self):
        group = ["test_verification", "wa", "wb", "wc"]
        weights = {"test_verification": 40, "wa": 30, "wb": 20, "wc": 10}
        script = _WorkerScript(
            deadlines={"test_verification": 4},
            records={module: json.dumps(
                {"complete": True, "python": "fake", "os": "macos",
                 "summary": {"ran": 1, "was_successful": True},
                 "modules": {module: {"tests": 1}}, "tests": []})
                for module in group})
        with tempfile.TemporaryDirectory() as tmp:
            args = run_test_shards.parse_args(
                ["--shard", "0", "--workers", "3", "--record", tmp])
            with mock.patch.object(run_test_shards.subprocess, "Popen",
                                   side_effect=script.popen), \
                    mock.patch.object(run_test_shards.platform, "system",
                                      return_value="Darwin"), \
                    contextlib.redirect_stdout(io.StringIO()):
                code = run_test_shards.run_shard_workers(
                    0, 5, group, weights, args)
            merged = json.loads(
                (Path(tmp) / "shard-0-of-5.json").read_text())
        self.assertEqual(code, 0)
        # On macOS the verification module is a normal pool member: it runs
        # alongside the other workers while it is still alive.
        self.assertTrue(any("test_verification" in proc.peers
                            for proc in script.launched))
        self.assertEqual(script.max_alive, 3)
        self.assertEqual(merged["exclusive"], [])

    def test_shard_workers_exit_codes_and_missing_ran_lines(self):
        weights = {"wa": 10, "wb": 5}
        args = run_test_shards.parse_args(["--shard", "0", "--workers", "2"])
        cases = [("a child exit 1 fails the shard",
                  _WorkerScript(exits={"wb": 1}), 1),
                 ("a child log without a Ran line fails the shard",
                  _WorkerScript(logs={"wb": "no summary here\n"}), 1),
                 ("clean children pass", _WorkerScript(), 0)]
        for name, script, expected in cases:
            with self.subTest(case=name):
                out = io.StringIO()
                with mock.patch.object(run_test_shards.subprocess, "Popen",
                                       side_effect=script.popen), \
                        contextlib.redirect_stdout(out):
                    code = run_test_shards.run_shard_workers(
                        0, 5, ["wa", "wb"], weights, args)
                self.assertEqual(code, expected)
                self.assertIn("total: ", out.getvalue())
                if expected:
                    self.assertIn("=== wb: exit ", out.getvalue())

    def test_worker_log_with_non_utf8_bytes_still_summarizes(self):
        weights = {"wa": 10}
        args = run_test_shards.parse_args(["--shard", "0", "--workers", "2"])
        script = _WorkerScript(
            logs={"wa": b"\xff\xfe\nRan 1 test in 0.001s\n\nOK\n"})
        out = io.StringIO()
        with mock.patch.object(run_test_shards.subprocess, "Popen",
                               side_effect=script.popen), \
                contextlib.redirect_stdout(out):
            code = run_test_shards.run_shard_workers(
                0, 5, ["wa"], weights, args)
        self.assertEqual(code, 0)
        self.assertIn("Ran 1 test in 0.001s", out.getvalue())

    def test_workers_one_keeps_the_single_process_shard_command(self):
        modules = run_test_shards.test_modules()
        groups, _loads, _missing = run_test_shards.assign(
            modules, 5, run_test_shards.load_weights())
        for argv in (["--shard", "1", "--shards", "5"],
                     ["--shard", "1", "--shards", "5", "--workers", "1"]):
            runner = mock.Mock(return_value=mock.Mock(returncode=0))
            with mock.patch.object(run_test_shards.subprocess, "run", runner):
                code = run_test_shards.main(argv)
            self.assertEqual(code, 0)
            self.assertEqual(runner.call_args.args[0],
                             run_test_shards.unittest_cmd(groups[1], False))
            self.assertIsNone(runner.call_args.kwargs["env"])

    def test_jobs_mode_with_workers_runs_each_shard_as_a_pool(self):
        def fake_popen(argv, **kwargs):
            # A pool child's log ends with the module table and `total: N`.
            kwargs["stdout"].write("=== wm: exit 0, 0.1s ===\n"
                                   "Ran 1 tests in 0.001s\nOK\n"
                                   "total: 1 tests\n")
            return mock.Mock(wait=lambda: 0)

        with mock.patch.object(run_test_shards.subprocess, "Popen",
                               side_effect=fake_popen) as popen, \
                contextlib.redirect_stdout(io.StringIO()):
            code = run_test_shards.main(["--jobs", "5", "--workers", "3"])
        self.assertEqual(code, 0)
        self.assertEqual(len(popen.call_args_list), 5)
        for index, call in enumerate(popen.call_args_list):
            argv = call.args[0]
            self.assertEqual(argv[:5], [sys.executable, "-B",
                                        str(ROOT / "tests" / "run_test_shards.py"),
                                        "--shard", str(index)])
            self.assertEqual(argv[argv.index("--workers") + 1], "3")

    def test_jobs_mode_summarizes_pool_children_by_their_total_line(self):
        # A pool child's log ends with a per-module table and `total: N` --
        # its last `Ran` line is only the last module's.
        def fake_popen(argv, **kwargs):
            kwargs["stdout"].write(
                "=== test_a: exit 0, 1.0s ===\n"
                "Ran 12 tests in 1.0s\nOK\n"
                "=== test_b: exit 0, 1.0s ===\n"
                "Ran 8 tests in 1.0s\nOK\n"
                "total: 20 tests\n")
            return mock.Mock(wait=lambda: 0)

        out = io.StringIO()
        with mock.patch.object(run_test_shards.subprocess, "Popen",
                               side_effect=fake_popen), \
                contextlib.redirect_stdout(out):
            code = run_test_shards.main(["--jobs", "5", "--workers", "2"])
        self.assertEqual(code, 0)
        text = out.getvalue()
        # Every shard row reports the pool total (20), not the last module's
        # Ran line (8), and the suite total sums all five shards.
        self.assertEqual(text.count("total: 20 tests"), 5)
        self.assertNotIn("Ran 8 tests", text)
        self.assertIn("total: 100 tests", text)

    def test_workers_requires_a_shard_mode(self):
        with self.assertRaises(SystemExit) as ctx:
            run_test_shards.main(["--list", "--workers", "3"])
        self.assertEqual(ctx.exception.code, 2)
        with self.assertRaises(SystemExit) as ctx:
            run_test_shards.main(["--shard", "0", "--workers", "0"])
        self.assertEqual(ctx.exception.code, 2)

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
