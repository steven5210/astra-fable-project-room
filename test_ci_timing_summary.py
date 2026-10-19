"""Exercise tests/ci_timing_summary.py against synthetic records and a
synthetic GitHub jobs payload: untrusted-input handling (oversize, wrong
format, non-JSON), the queue/setup/exec definitions, record-to-job joining,
the per-OS imbalance, the slowest-module table and the bounded combined JSON.
Everything is faked on disk; nothing touches the network.
"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "ci_timing_summary", ROOT / "tests" / "ci_timing_summary.py")
ci_summary = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ci_summary)


def record(os_name, python, shard, complete=True, modules=None, ran=10,
           truncated=False):
    data = {"format": "project_room_test_timing_v1", "complete": complete,
            "os": os_name, "python_minor": python, "python": python + ".0",
            "shard": {"index": shard, "count": 5},
            "revision": "abc1234", "run_id": "4242",
            "summary": {"ran": ran, "was_successful": True},
            "modules": modules or {}, "tests": [{"id": "x.y.z"}]}
    if truncated:
        data["tests_truncated"] = True
    return data


def job(name, created, started, completed, steps, conclusion="success"):
    return {"name": name, "created_at": created, "started_at": started,
            "completed_at": completed, "conclusion": conclusion, "steps": steps}


def step(name, started, completed, conclusion="success"):
    return {"name": name, "conclusion": conclusion,
            "started_at": started, "completed_at": completed}


T = "2026-10-17T00:{:02d}:{:02d}Z".format


class CiTimingSummaryTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp = Path(self._tmpdir.name)
        self.records_dir = self.tmp / "records"
        self.records_dir.mkdir()

    def _write_record(self, artifact, filename, data):
        directory = self.records_dir / artifact
        directory.mkdir(parents=True, exist_ok=True)
        (directory / filename).write_text(json.dumps(data))

    def _write_jobs(self, pages):
        path = self.tmp / "jobs.json"
        # gh api --paginate concatenates the page objects.
        path.write_text("\n".join(json.dumps(page) for page in pages))
        return path

    def _run(self):
        markdown = self.tmp / "summary.md"
        combined = self.tmp / "combined.json"
        jobs_path = self.tmp / "jobs.json"
        code = ci_summary.main([
            "--records", str(self.records_dir), "--jobs", str(jobs_path),
            "--combined", str(combined), "--markdown", str(markdown)])
        self.assertEqual(code, 0)
        return markdown.read_text(), json.loads(combined.read_text())

    def _fixtures(self):
        self._write_record("timing-macos-latest-py3.11-shard0", "shard-0-of-5.json", record(
            "macos", "3.11", 0, ran=25, modules={
                "test_heavy": {"tests": 9, "wall_seconds": 120, "test_seconds": 90,
                               "setup_seconds": 60, "fixture_gap_seconds": 30},
                "test_light": {"tests": 3, "wall_seconds": 10, "test_seconds": 9,
                               "setup_seconds": 3, "fixture_gap_seconds": 1}}))
        self._write_record("timing-macos-latest-py3.11-shard1", "shard-1-of-5.json", record(
            "macos", "3.11", 1, ran=18, modules={
                "test_mid": {"tests": 5, "wall_seconds": 60, "test_seconds": 55,
                             "setup_seconds": 20, "fixture_gap_seconds": 5}}))
        self._write_record("timing-ubuntu-latest-py3.11-shard0", "shard-0-of-5.json", record(
            "linux", "3.11", 0, complete=False, ran=18, modules={
                "test_linux": {"tests": 4, "wall_seconds": 80, "test_seconds": 70,
                               "setup_seconds": 25, "fixture_gap_seconds": 10}}))
        (self.records_dir / "oversized.json").write_text(" " * (9 * 1024 * 1024))
        (self.records_dir / "badformat.json").write_text(json.dumps({"format": "other"}))
        (self.records_dir / "notjson.json").write_text("this is not json {")

        jobs = [
            job("unittest (macos-latest, 3.11, 0)",
                "2026-10-17T00:00:00Z", "2026-10-17T00:01:00Z", "2026-10-17T00:11:30Z",
                [step("Set up job", "2026-10-17T00:01:00Z", "2026-10-17T00:01:10Z"),
                 step("Run macOS tests with fake model and MCP backends",
                      "2026-10-17T00:01:10Z", "2026-10-17T00:11:10Z")]),
            job("unittest (macos-latest, 3.11, 1)",
                "2026-10-17T00:00:00Z", "2026-10-17T00:01:00Z", "2026-10-17T00:06:30Z",
                [step("Run macOS tests with fake model and MCP backends",
                      "2026-10-17T00:01:10Z", "2026-10-17T00:06:10Z")]),
            job("unittest (ubuntu-latest, 3.11, 0)",
                "2026-10-17T00:00:00Z", "2026-10-17T00:02:00Z", "2026-10-17T00:11:00Z",
                [step("Run macOS tests with fake model and MCP backends",
                      "2026-10-17T00:02:10Z", "2026-10-17T00:02:20Z",
                      conclusion="skipped"),
                 step("Run Linux tests in an isolated process namespace",
                      "2026-10-17T00:02:30Z", "2026-10-17T00:10:50Z")]),
        ]
        more_jobs = [
            job("unittest (ubuntu-latest, 3.11, 4)",
                "2026-10-17T00:00:00Z", None, None,
                [step("Run Linux tests in an isolated process namespace",
                      "2026-10-17T00:03:00Z", "2026-10-17T00:08:00Z")]),
            job("unittest (ubuntu-latest, 3.12, 2)",
                "2026-10-17T00:00:00Z", "2026-10-17T00:00:30Z", "2026-10-17T00:05:00Z",
                [step("Run Linux tests in an isolated process namespace",
                      "2026-10-17T00:00:40Z", "2026-10-17T00:04:40Z")]),
            job("lint",
                "2026-10-17T00:00:00Z", "2026-10-17T00:00:05Z", "2026-10-17T00:01:00Z",
                [step("Pyflakes", "2026-10-17T00:00:30Z", "2026-10-17T00:00:55Z")]),
        ]
        self._write_jobs([{"total_count": 6, "jobs": jobs},
                          {"total_count": 6, "jobs": more_jobs}])

    def test_full_summary(self):
        self._fixtures()
        markdown, combined = self._run()

        self.assertIn("## Test timing summary", markdown)
        self.assertIn("revision: `abc1234`", markdown)
        self.assertIn("jobs: 6", markdown)
        self.assertIn("3 found (2 complete, 1 incomplete)", markdown)
        self.assertIn("ignored files: 3", markdown)
        for name in ("oversized.json", "badformat.json", "notjson.json"):
            self.assertIn(name, markdown, name)

        # Job rows: queue = started - created, setup = test-step start - job
        # start (the skipped macOS step on the ubuntu job is passed over),
        # exec = test-step duration, total = completed - started.
        self.assertIn("| unittest (macos-latest, 3.11, 0) | macos | 3.11 | 0 |"
                      " 1.0m | 0.2m | 10.0m | 10.5m | success | 25 | complete |",
                      markdown)
        self.assertIn("| unittest (ubuntu-latest, 3.11, 0) | linux | 3.11 | 0 |"
                      " 2.0m | 0.5m | 8.3m | 9.0m | success | 18 | incomplete |",
                      markdown)
        # Missing job timestamps degrade to n/a; the step's own times still
        # give a measured exec.
        self.assertIn("| unittest (ubuntu-latest, 3.11, 4) | linux | 3.11 | 4 |"
                      " n/a | n/a | 5.0m | n/a | success | n/a | missing |",
                      markdown)
        self.assertIn("| lint | - | - | - | 0.1m | n/a | n/a | 0.9m | success | - | - |",
                      markdown)

        self.assertIn("macos/3.11: exec max 10.0m, median 7.5m, max/median 1.33",
                      markdown)
        self.assertIn("linux/3.12: exec max 4.0m, median 4.0m, max/median 1.00",
                      markdown)
        self.assertIn("runner-minutes (unittest + lint):", markdown)

        self.assertIn("#### macos/3.11", markdown)
        self.assertIn("| test_heavy | 120s | 90s | 67% | 30s |", markdown)
        self.assertIn("| test_light | 10s | 9s | 33% | 1s |", markdown)

        self.assertIn("incomplete record `timing-ubuntu-latest-py3.11-shard0/shard-0-of-5.json`",
                      markdown)
        self.assertIn("unittest (ubuntu-latest, 3.12, 2)", markdown)
        self.assertIn("unittest (ubuntu-latest, 3.11, 4)", markdown)
        self.assertIn("jobs without records", markdown)

        self.assertEqual(combined["format"], "project_room_timing_combined_v1")
        self.assertEqual(combined["revision"], "abc1234")
        self.assertEqual(combined["run_id"], "4242")
        self.assertEqual(len(combined["jobs"]), 6)
        mac_shard0 = next(job_ for job_ in combined["jobs"]
                          if job_["name"] == "unittest (macos-latest, 3.11, 0)")
        self.assertEqual(mac_shard0["queue_seconds"], 60.0)
        self.assertEqual(mac_shard0["setup_seconds"], 10.0)
        self.assertEqual(mac_shard0["exec_seconds"], 600.0)
        self.assertEqual(mac_shard0["total_seconds"], 630.0)
        self.assertEqual(set(mac_shard0["record"].keys()),
                         {"modules", "summary", "shard", "complete"})
        self.assertIn("test_heavy", mac_shard0["record"]["modules"])
        self.assertEqual(mac_shard0["record"]["summary"]["ran"], 25)
        self.assertFalse(any("tests" in job_["record"] for job_ in combined["jobs"]
                             if job_["record"]))
        ubuntu_312 = next(job_ for job_ in combined["jobs"]
                          if job_["name"] == "unittest (ubuntu-latest, 3.12, 2)")
        self.assertIsNone(ubuntu_312["record"])

    def test_paginated_jobs_file_is_decoded(self):
        self._fixtures()
        jobs = ci_summary.load_jobs(self.tmp / "jobs.json")
        self.assertEqual(len(jobs), 6)

    def test_missing_jobs_file_still_writes_a_summary(self):
        (self.records_dir / "a").mkdir()
        self._write_record("a", "shard-0-of-5.json", record("macos", "3.11", 0))
        markdown, combined = self._run()
        self.assertIn("unreadable jobs file", markdown)
        self.assertEqual(combined["jobs"], [])

    def test_header_falls_back_to_environment_provenance(self):
        self._write_jobs([{"jobs": []}])
        env = {"PROJECT_ROOM_TIMING_REVISION": "envrev42",
               "GITHUB_RUN_ID": "98765"}
        with mock.patch.dict(os.environ, env, clear=False):
            markdown, combined = self._run()
        self.assertIn("revision: `envrev42`", markdown)
        self.assertIn("run id: `98765`", markdown)
        self.assertEqual(combined["revision"], "envrev42")
        self.assertEqual(combined["run_id"], "98765")

    def test_a_record_with_wrong_field_types_is_ignored(self):
        (self.records_dir / "a").mkdir()
        self._write_record("a", "bad.json", {"format": "project_room_test_timing_v1",
                                             "modules": [1, 2], "summary": {}})
        self._write_jobs([{"jobs": []}])
        markdown, _ = self._run()
        self.assertIn("ignored `a/bad.json`: wrong format or field types", markdown)

    def test_workers_subtree_records_are_ignored(self):
        # A local --record run leaves per-module worker records under
        # workers/shard-I-of-N/; only the merged shard record counts.
        self._write_record("a", "shard-4-of-5.json",
                           record("macos", "3.11", 4, ran=100))
        self._write_record("a/workers/shard-4-of-5", "test_one.json",
                           record("macos", "3.11", 4, ran=1))
        self._write_record("a/workers/shard-4-of-5", "test_two.json",
                           record("macos", "3.11", 4, ran=1))
        self._write_jobs([{"jobs": []}])
        markdown, _ = self._run()
        self.assertIn("records: 1 found", markdown)
        self.assertNotIn("test_one.json", markdown)
        self.assertNotIn("test_two.json", markdown)


def baseline_job(module_walls, exec_seconds=300, total_seconds=500,
                 os_name="macos", python="3.11", shard=0):
    return {"name": f"unittest ({os_name}-latest, {python}, {shard})",
            "os": os_name, "python": python, "shard": shard,
            "conclusion": "success",
            "exec_seconds": exec_seconds, "total_seconds": total_seconds,
            "record": {"complete": True,
                       "modules": {module: (wall if isinstance(wall, dict)
                                            else {"wall_seconds": wall})
                                   for module, wall in module_walls.items()}}}


class BaselineComparisonTests(unittest.TestCase):
    """Drift comparison against rolling main baselines and the weights file."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp = Path(self._tmpdir.name)
        self.records_dir = self.tmp / "records"
        self.records_dir.mkdir()
        self.baselines_dir = self.tmp / "baselines"
        self.baselines_dir.mkdir()

    def _write_record(self, artifact, filename, data):
        directory = self.records_dir / artifact
        directory.mkdir(parents=True, exist_ok=True)
        (directory / filename).write_text(json.dumps(data))

    def _write_baseline(self, run_id, jobs):
        (self.baselines_dir / f"baseline-{run_id}.json").write_text(
            json.dumps({"format": "project_room_timing_combined_v1",
                        "run_id": str(run_id), "revision": f"r{run_id}",
                        "generated_at": "2026-10-17T00:00:00Z",
                        "jobs": jobs}))

    def _write_weights(self, weights, samples):
        path = self.tmp / "weights.json"
        path.write_text(json.dumps(
            {"_meta": {"format": "project_room_shard_weights_v2"},
             "weights": weights, "samples": samples}))
        return path

    def _write_jobs(self, jobs):
        path = self.tmp / "jobs.json"
        path.write_text(json.dumps({"jobs": jobs}))
        return path

    def _run(self, baselines=True, weights=None, capture=False):
        markdown = self.tmp / "summary.md"
        combined = self.tmp / "combined.json"
        argv = ["--records", str(self.records_dir),
                "--jobs", str(self.tmp / "jobs.json"),
                "--combined", str(combined), "--markdown", str(markdown)]
        if baselines:
            argv += ["--baselines", str(self.baselines_dir)]
        if weights is not None:
            argv += ["--weights", str(weights)]
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = ci_summary.main(argv)
        self.assertEqual(code, 0)
        result = (markdown.read_text(), json.loads(combined.read_text()))
        return result + (stdout.getvalue(),) if capture else result

    def _fixtures(self):
        """Current run: test_a 250s, test_b 200s, test_c 130s, test_stale
        100s, test_local 50s, test_missing 70s on macOS; shard execs 60s and
        480s (imbalanced), plus linux/3.12 execs 20s and 80s (ratio 1.6 but
        only a 60s gap)."""
        self._write_record("timing-macos-latest-py3.11-shard0",
                           "shard-0-of-5.json", record(
                               "macos", "3.11", 0, modules={
                                   "test_a": {"wall_seconds": 250},
                                   "test_b": {"wall_seconds": 200},
                                   "test_c": {"wall_seconds": 130},
                                   "test_stale": {"wall_seconds": 100},
                                   "test_local": {"wall_seconds": 50},
                                   "test_missing": {"wall_seconds": 70}}))
        self._write_record("timing-ubuntu-latest-py3.12-shard0",
                           "shard-0-of-5.json", record(
                               "linux", "3.12", 0, modules={
                                   "test_c": {"wall_seconds": 60}}))
        self._write_jobs([
            job("unittest (macos-latest, 3.11, 0)", "2026-10-17T00:00:00Z",
                "2026-10-17T00:01:00Z", "2026-10-17T00:09:00Z",
                [step("Run macOS tests", "2026-10-17T00:01:10Z",
                      "2026-10-17T00:02:10Z")]),
            job("unittest (macos-latest, 3.11, 1)", "2026-10-17T00:00:00Z",
                "2026-10-17T00:01:00Z", "2026-10-17T00:09:00Z",
                [step("Run macOS tests", "2026-10-17T00:01:00Z",
                      "2026-10-17T00:09:00Z")]),
            job("unittest (ubuntu-latest, 3.12, 0)", "2026-10-17T00:00:00Z",
                "2026-10-17T00:01:00Z", "2026-10-17T00:03:00Z",
                [step("Run Linux tests", "2026-10-17T00:01:00Z",
                      "2026-10-17T00:01:20Z")]),
            job("unittest (ubuntu-latest, 3.12, 1)", "2026-10-17T00:00:00Z",
                "2026-10-17T00:01:00Z", "2026-10-17T00:03:00Z",
                [step("Run Linux tests", "2026-10-17T00:01:00Z",
                      "2026-10-17T00:02:20Z")]),
        ])
        # Five baseline runs, 105 newest: test_a regressed on the two newest
        # (250 vs older 100), test_b measured by only two runs, test_c a
        # steady 100.
        self._write_baseline(105, [baseline_job({"test_a": 250, "test_c": 100},
                                                exec_seconds=400)])
        self._write_baseline(104, [baseline_job({"test_a": 250, "test_c": 100},
                                                exec_seconds=380)])
        self._write_baseline(103, [baseline_job(
            {"test_a": 100, "test_b": 100, "test_c": 100}, exec_seconds=300)])
        self._write_baseline(102, [baseline_job(
            {"test_a": 100, "test_b": 100, "test_c": 100}, exec_seconds=310)])
        self._write_baseline(101, [baseline_job({"test_a": 100, "test_c": 100},
                                                exec_seconds=290)])
        weights = {"test_a": 100, "test_b": 100, "test_c": 100,
                   "test_stale": 40, "test_local": 50, "test_fine": 60}
        samples = {"test_a": 3, "test_b": 3, "test_c": 3, "test_stale": 3,
                   "test_local": 0, "test_fine": 2}
        return self._write_weights(weights, samples)

    def test_module_drift_imbalance_and_weights(self):
        weights_path = self._fixtures()
        markdown, combined, stdout = self._run(weights=weights_path,
                                               capture=True)

        self.assertIn("### Warnings", markdown)
        # (a) test_a: 2.5x baseline, n=5, newest two runs also high.
        self.assertIn("`test_a` 250s wall vs baseline median 100s wall"
                      " (n=5 runs, range 100s-250s) -- sustained", markdown)
        # (b) test_b: exceeds the magnitude but n=2 -> inconclusive only.
        self.assertIn("inconclusive (n=2 wall): slower than baseline"
                      " `test_b`", markdown)
        self.assertNotIn("warning title=Test timing::module slower than "
                         "baseline: test_b", stdout)
        # (c) test_c: 1.3x -> neither warning nor inconclusive.
        self.assertNotIn("test_c` 130s", markdown)
        # (d) macOS imbalance 1.78 with a 420s gap warns; linux/3.12 at
        # ratio 1.6 with a 60s gap does not.
        self.assertIn("shard imbalance macos/3.11: max/median 1.78,"
                      " slowest shard 210s over median", markdown)
        self.assertNotIn("imbalance linux/3.12", markdown)
        # (e) weights: stale lists test_a (250 vs 100) and test_stale
        # (100 vs 40); test_local is samples=0 and measured -> notice;
        # test_missing has no weight.
        self.assertIn("stale weight estimates: `test_a`, `test_stale`",
                      markdown)
        self.assertIn("local weight estimates measured on CI: `test_local`",
                      markdown)
        self.assertIn("modules without a pinned weight: `test_missing`",
                      markdown)
        self.assertIn("--refresh-weights <timing-combined.json ...>", markdown)
        self.assertIn("--measure test_missing", markdown)

        # (f) annotations: each warning/notice once, capped at 10.
        annotations = [line for line in stdout.splitlines()
                       if line.startswith("::warning") or line.startswith("::notice")]
        self.assertLessEqual(len(annotations), 10)
        self.assertTrue(any("module slower than baseline" in a
                            and a.startswith("::warning title=Test timing::")
                            for a in annotations))
        self.assertTrue(any(a.startswith("::notice title=Test timing::")
                            and "test_local" in a for a in annotations))

        # Baseline context on the imbalance and runner-minutes lines.
        self.assertIn("macos/3.11: exec max 8.0m, median 4.5m,"
                      " max/median 1.78, sum 9.0m; baseline max 5.2m,"
                      " median 5.2m (n=5 runs, range 4.8m-6.7m)", markdown)
        self.assertIn("runner-minutes (unittest + lint): 20"
                      " vs baseline median 8 (n=5 runs, range 8-8)",
                      markdown)
        self.assertIn("linux/3.12: exec max 1.3m", markdown)
        self.assertIn("baseline: none", markdown)  # no linux/3.12 baseline

        # (g) combined JSON carries the runs used and the module count.
        self.assertEqual(combined["baseline"]["runs"],
                         ["105", "104", "103", "102", "101"])
        self.assertEqual(combined["baseline"]["modules_n"], 3)

    def test_annotations_capped_at_ten(self):
        slow = {f"test_m{i}": {"wall_seconds": 300} for i in range(12)}
        self._write_record("a", "shard-0-of-5.json",
                           record("macos", "3.11", 0, modules=slow))
        self._write_jobs([])
        for run_id in range(101, 106):
            self._write_baseline(run_id, [baseline_job(
                {module: 100 for module in slow})])
        _, _, stdout = self._run(capture=True)
        annotations = [line for line in stdout.splitlines()
                       if line.startswith("::warning")]
        self.assertEqual(len(annotations), 10)

    def test_no_baselines_dir_degrades(self):
        self._write_record("a", "shard-0-of-5.json",
                           record("macos", "3.11", 0))
        self._write_jobs([])
        markdown, combined = self._run(baselines=False)
        self.assertIn("### Warnings\n\n- none", markdown)
        self.assertIn("baseline: none", markdown)
        self.assertEqual(combined["baseline"], {"runs": [], "modules_n": 0})

    def test_empty_baselines_dir_degrades_to_note(self):
        self._write_record("a", "shard-0-of-5.json",
                           record("macos", "3.11", 0))
        self._write_jobs([])
        markdown, combined = self._run()  # dir exists but empty
        self.assertIn("no usable baselines", markdown)
        self.assertIn("baseline: none", markdown)
        self.assertEqual(combined["baseline"]["runs"], [])

    def test_sustained_requires_the_two_newest_runs_to_be_high(self):
        # test_x is slow now (250 vs median 100, n=5) but the two newest
        # baseline runs are fast -- the warning stands without "sustained".
        self._write_record("a", "shard-0-of-5.json", record(
            "macos", "3.11", 0, modules={"test_x": {"wall_seconds": 250}}))
        self._write_jobs([])
        for run_id, wall in [(109, 100), (108, 100), (107, 100),
                             (106, 100), (105, 250)]:
            self._write_baseline(run_id, [baseline_job({"test_x": wall})])
        markdown, _ = self._run()
        self.assertIn("`test_x` 250s wall vs baseline median 100s wall"
                      " (n=5 runs", markdown)
        self.assertNotIn("sustained", markdown)

    def test_sustained_requires_the_kind_in_the_two_newest_runs(self):
        # Five process runs sit under two newer wall-only runs: the process
        # series is five deep but its two newest entries are not the two
        # newest main runs -- the warning stands without "sustained".
        self._write_record("a", "shard-0-of-5.json", record(
            "macos", "3.11", 0, modules={
                "test_x": {"wall_seconds": 60, "process_seconds": 250}}))
        self._write_jobs([])
        for run_id in (107, 106):  # the two newest runs: wall only
            self._write_baseline(run_id, [baseline_job({"test_x": 100})])
        for run_id, process in [(105, 250), (104, 250), (103, 100),
                                (102, 100), (101, 100)]:
            self._write_baseline(run_id, [baseline_job(
                {"test_x": {"wall_seconds": 60,
                            "process_seconds": process}})])
        markdown, _ = self._run()
        self.assertIn("module slower than baseline: `test_x` 250s process"
                      " vs baseline median 100s process (n=5 runs", markdown)
        self.assertNotIn("sustained", markdown)

    def test_runner_minutes_excludes_the_timing_summary_job(self):
        self._write_record("a", "shard-0-of-5.json",
                           record("macos", "3.11", 0))
        self._write_jobs([
            job("unittest (macos-latest, 3.11, 0)", "2026-10-17T00:00:00Z",
                "2026-10-17T00:01:00Z", "2026-10-17T00:11:00Z",
                [step("Run tests", "2026-10-17T00:01:00Z",
                      "2026-10-17T00:11:00Z")]),
            # In-progress summary job: its unfinished total must not count.
            job("timing-summary", "2026-10-17T00:10:00Z",
                "2026-10-17T00:11:00Z", None, []),
        ])
        # A completed baseline summary job (900 s) must not inflate the
        # baseline runner-minutes either.
        self._write_baseline(101, [
            baseline_job({}, total_seconds=500),
            {"name": "timing-summary", "os": None, "python": None,
             "conclusion": "success", "exec_seconds": 100,
             "total_seconds": 900}])
        markdown, _ = self._run()
        # 600 s current, 500 s baseline -- the summary job is out on both.
        self.assertIn("runner-minutes (unittest + lint): 10"
                      " vs baseline median 8", markdown)

    def test_slowest_table_shows_process_seconds_when_present(self):
        self._write_record("a", "shard-0-of-5.json", record(
            "macos", "3.11", 0, modules={
                "test_pooled": {"wall_seconds": 90, "test_seconds": 80,
                                "process_seconds": 120}}))
        self._write_jobs([{"jobs": []}])
        markdown, _ = self._run(baselines=False)
        self.assertIn("| module | cost (process|wall) | wall | process"
                      " | test |", markdown)
        self.assertIn("| test_pooled | 120s | 90s | 120s | 80s |", markdown)

    def test_slowest_table_ranks_by_process_seconds_when_present(self):
        # Cost is process_seconds when the pool stamped it: a module with
        # 1 s of module wall but 300 s of process wall outranks a 200 s
        # wall-only module.
        self._write_record("a", "shard-0-of-5.json", record(
            "macos", "3.11", 0, modules={
                "test_big_wall": {"wall_seconds": 200, "test_seconds": 190},
                "test_small_wall": {"wall_seconds": 1, "test_seconds": 1,
                                    "process_seconds": 300}}))
        self._write_jobs([{"jobs": []}])
        markdown, _ = self._run(baselines=False)
        self.assertLess(markdown.index("test_small_wall"),
                        markdown.index("test_big_wall"))

    def test_drift_compares_only_the_same_measurement_kind(self):
        # A pooled current run measures process seconds; wall-only
        # baselines are a different span, so the module is inconclusive,
        # never warned.
        self._write_record("a", "shard-0-of-5.json", record(
            "macos", "3.11", 0, modules={
                "test_p": {"wall_seconds": 50, "process_seconds": 250}}))
        self._write_jobs([])
        for run_id in range(101, 106):
            self._write_baseline(run_id, [baseline_job({"test_p": 100})])
        markdown, _ = self._run()
        self.assertIn("inconclusive: no process baseline runs for 1 module"
                      " (measurement kind changed", markdown)
        self.assertNotIn("module slower than baseline", markdown)

        # The same current value against process baselines warns as before.
        for run_id in range(101, 106):
            self._write_baseline(run_id, [baseline_job(
                {"test_p": {"wall_seconds": 80, "process_seconds": 100}})])
        markdown, _ = self._run()
        self.assertIn("module slower than baseline: `test_p` 250s process"
                      " vs baseline median 100s process (n=5 runs", markdown)

    def test_unreadable_weights_degrades_to_note(self):
        self._write_record("a", "shard-0-of-5.json",
                           record("macos", "3.11", 0))
        self._write_jobs([])
        markdown, _ = self._run(baselines=False,
                                weights=self.tmp / "absent.json")
        self.assertIn("weights file unreadable", markdown)


if __name__ == "__main__":
    unittest.main()
