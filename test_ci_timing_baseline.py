"""Exercise tests/ci_timing_baseline.py with an injected fake `api`: run
listing (newest first, skip-run-id), artifact selection (missing, expired),
in-memory zip extraction (extra and ../evil members ignored, oversize, corrupt),
the --max-runs limit, notes for skipped runs and exit 0 in every failure path.
Nothing touches the network or `gh`.
"""
import importlib.util
import io
import json
import tempfile
from pathlib import Path
import unittest
import zipfile

ROOT = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "ci_timing_baseline", ROOT / "tests" / "ci_timing_baseline.py")
ci_baseline = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ci_baseline)


def combined(revision="rev", generated="2026-10-17T00:00:00Z", run_id="1"):
    return {"format": "project_room_timing_combined_v1", "run_id": run_id,
            "revision": revision, "generated_at": generated, "jobs": []}


def make_zip(member_name="timing-combined.json", payload=None, extra=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(member_name, payload or json.dumps(combined()))
        for name, body in (extra or {}).items():
            archive.writestr(name, body)
    return buffer.getvalue()


class FakeApi:
    """Dispatch on the gh-api path; `binary` is asserted like the real call."""

    def __init__(self):
        self.calls = []
        self.runs = {"workflow_runs": []}
        self.artifacts = {}
        self.zips = {}
        self.fail_runs_listing = None

    def __call__(self, path, binary=False):
        self.calls.append(path)
        if "/workflows/" in path:
            if self.fail_runs_listing:
                raise self.fail_runs_listing
            return json.dumps(self.runs)
        if path.endswith("/artifacts"):
            run_id = int(path.split("/runs/")[1].split("/")[0])
            return json.dumps({"artifacts": self.artifacts.get(run_id, [])})
        if "/artifacts/" in path and path.endswith("/zip"):
            assert binary, "zip must be fetched as bytes"
            artifact_id = int(path.split("/artifacts/")[1].split("/")[0])
            data = self.zips[artifact_id]
            if isinstance(data, Exception):
                raise data
            return data
        raise AssertionError(f"unexpected api path: {path}")


def run_entry(run_id):
    return {"id": run_id, "status": "completed"}


def artifact(artifact_id, name="timing-summary", expired=False,
             size_in_bytes=1024):
    return {"id": artifact_id, "name": name, "expired": expired,
            "size_in_bytes": size_in_bytes}


class CiTimingBaselineTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp = Path(self._tmpdir.name)
        self.out = self.tmp / "baselines"
        self.api = FakeApi()

    def _run(self, *extra):
        code = ci_baseline.main(
            ["--repo", "o/n", "--workflow", "tests.yml",
             "--out", str(self.out), *extra], api=self.api)
        self.assertEqual(code, 0)

    def test_fetches_newest_first_skipping_skip_run_id(self):
        self.api.runs = {"workflow_runs":
                         [run_entry(90), run_entry(80), run_entry(70)]}
        self.api.artifacts = {80: [artifact(8)], 70: [artifact(7)]}
        self.api.zips = {8: make_zip(payload=json.dumps(combined("r80", run_id="80"))),
                         7: make_zip(payload=json.dumps(combined("r70", run_id="70")))}
        self._run("--skip-run-id", "90")
        self.assertEqual(sorted(p.name for p in self.out.glob("baseline-*.json")),
                         ["baseline-70.json", "baseline-80.json"])
        data = json.loads((self.out / "baseline-80.json").read_bytes())
        self.assertEqual(data["revision"], "r80")
        # Run 90 is skipped before its artifacts are even listed.
        self.assertFalse(any("/runs/90/" in c for c in self.api.calls))

    def test_max_runs_caps_fetches_in_order(self):
        self.api.runs = {"workflow_runs": [run_entry(i) for i in (3, 2, 1)]}
        for run_id, aid in ((3, 13), (2, 12), (1, 11)):
            self.api.artifacts[run_id] = [artifact(aid)]
            self.api.zips[aid] = make_zip()
        self._run("--max-runs", "2")
        self.assertEqual(sorted(p.name for p in self.out.glob("baseline-*.json")),
                         ["baseline-2.json", "baseline-3.json"])
        self.assertFalse(any("/runs/1/" in c for c in self.api.calls))

    def test_runs_without_artifact_or_expired_are_noted(self):
        self.api.runs = {"workflow_runs": [run_entry(3), run_entry(2), run_entry(1)]}
        self.api.artifacts = {3: [artifact(13, expired=True)],
                              2: [],  # no timing-summary at all
                              1: [artifact(11)]}
        self.api.zips = {11: make_zip()}
        self._run()
        self.assertEqual([p.name for p in self.out.glob("baseline-*.json")],
                         ["baseline-1.json"])
        notes = (self.out / "notes.txt").read_text()
        self.assertIn("run 3: no timing-summary artifact", notes)
        self.assertIn("run 2: no timing-summary artifact", notes)

    def test_zip_extras_and_evil_members_are_never_paths(self):
        self.api.runs = {"workflow_runs": [run_entry(5)]}
        self.api.artifacts = {5: [artifact(15)]}
        self.api.zips = {15: make_zip(
            payload=json.dumps(combined("good")),
            extra={"../evil": "PWNED", "other.txt": "x",
                   "nested/dir.json": "{}"})}
        self._run()
        self.assertEqual(sorted(p.name for p in self.out.iterdir()),
                         ["baseline-5.json"])
        self.assertFalse((self.tmp / "evil").exists())
        self.assertFalse((self.out.parent / "evil").exists())
        self.assertEqual(json.loads((self.out / "baseline-5.json").read_bytes())
                         ["revision"], "good")

    def test_zip_without_member_oversize_member_and_corrupt_zip(self):
        self.api.runs = {"workflow_runs": [run_entry(3), run_entry(2), run_entry(1)]}
        self.api.artifacts = {3: [artifact(13)], 2: [artifact(12)], 1: [artifact(11)]}
        self.api.zips = {13: make_zip(member_name="wrong.json"),
                         12: make_zip(payload=" " * (9 * 1024 * 1024)),
                         11: b"this is not a zip"}
        self._run()
        self.assertEqual(list(self.out.glob("baseline-*.json")), [])
        notes = (self.out / "notes.txt").read_text()
        self.assertIn("run 3: no timing-combined.json member", notes)
        self.assertIn("run 2: timing-combined.json over 8 MiB", notes)
        self.assertIn("run 1: skipped", notes)

    def test_runs_listing_failure_writes_notes_and_exits_0(self):
        self.api.fail_runs_listing = RuntimeError("gh exploded")
        self._run()
        self.assertIn("cannot list runs: RuntimeError: gh exploded",
                      (self.out / "notes.txt").read_text())
        self.assertEqual(list(self.out.glob("baseline-*.json")), [])

    def test_non_numeric_run_id_is_skipped_before_any_path_use(self):
        self.api.runs = {"workflow_runs": [{"id": "../x"}, run_entry(1)]}
        self.api.artifacts = {1: [artifact(11)]}
        self.api.zips = {11: make_zip()}
        self._run()
        self.assertEqual([p.name for p in self.out.glob("baseline-*.json")],
                         ["baseline-1.json"])
        self.assertEqual(sorted(p.name for p in self.out.iterdir()),
                         ["baseline-1.json"])
        self.assertFalse(any("../x" in call for call in self.api.calls))

    def test_artifact_without_size_or_oversize_is_not_downloaded(self):
        self.api.runs = {"workflow_runs": [run_entry(3), run_entry(2),
                                         run_entry(1)]}
        self.api.artifacts = {3: [artifact(13, size_in_bytes=None)],
                              2: [artifact(12, size_in_bytes=20 * 1024 * 1024)],
                              1: [artifact(11)]}
        self.api.zips = {11: make_zip()}
        self._run()
        self.assertEqual([p.name for p in self.out.glob("baseline-*.json")],
                         ["baseline-1.json"])
        notes = (self.out / "notes.txt").read_text()
        self.assertIn("run 3: artifact size unknown or over 16 MiB", notes)
        self.assertIn("run 2: artifact size unknown or over 16 MiB", notes)
        self.assertFalse(any("/artifacts/13/zip" in c or "/artifacts/12/zip" in c
                             for c in self.api.calls))

    def test_bad_json_combined_is_skipped(self):
        self.api.runs = {"workflow_runs": [run_entry(1)]}
        self.api.artifacts = {1: [artifact(11)]}
        self.api.zips = {11: make_zip(payload="{not json")}
        self._run()
        self.assertEqual(list(self.out.glob("baseline-*.json")), [])
        self.assertIn("run 1: skipped", (self.out / "notes.txt").read_text())


if __name__ == "__main__":
    unittest.main()
