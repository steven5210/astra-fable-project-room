"""Exercise tests/ci_timing_summary.py against synthetic records and a
synthetic GitHub jobs payload: untrusted-input handling (oversize, wrong
format, non-JSON), the queue/setup/exec definitions, record-to-job joining,
the per-OS imbalance, the slowest-module table and the bounded combined JSON.
Everything is faked on disk; nothing touches the network.
"""
import importlib.util
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
        self.assertIn("runner-minutes:", markdown)

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


if __name__ == "__main__":
    unittest.main()
