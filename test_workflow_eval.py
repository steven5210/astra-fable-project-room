"""Behavioral tests for the workflow evaluation package (PR #227 review findings).

Binds correctness to the exact verified/accepted/probed candidate, deduplicates worker
launches across outcome observations, and treats a receipt as proof only for a settled
request state. Stdlib unittest; synthetic rooms and candidates only.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent
PACKAGE = ROOT / "evaluation" / "workflow-eval"
sys.path.insert(0, str(PACKAGE / "checks"))
sys.path.insert(0, str(PACKAGE))

import dry_run  # noqa: E402
import export_bundle  # noqa: E402
import probes  # noqa: E402
import score  # noqa: E402

T2_TASK = PACKAGE / "tasks" / "T2-helper-only-proof" / "task.json"
T1_TASK = PACKAGE / "tasks" / "T1-incomplete-handoff" / "task.json"
PROVIDER = '00000000-0000-4000-8000-000000000099'


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()


class WorkflowEvalTests(unittest.TestCase):
    def bound_room(self, temp, settled_review=True):
        """synthetic_room plus the verification checkpoint the binding contract reads."""
        room, repo, head = dry_run.synthetic_room(temp, settled_review=settled_review)
        state = json.loads((room / "state.json").read_text())
        verification = state["verifications"][-1]
        checkpoint = {"candidate_sha256": verification["candidate_sha256"],
                      "candidate": {"head": head}, "passed": True}
        path = "verification/" + verification["id"] + "/checkpoint.json"
        (room / path).parent.mkdir(parents=True, exist_ok=True)
        (room / path).write_text(json.dumps(checkpoint))
        verification["path"] = path
        state["acceptances"][-1]["evidence_sha256"] = hashlib.sha256(
            canonical(checkpoint)).hexdigest()
        (room / "state.json").write_text(json.dumps(state))
        return room, repo, head

    def export(self, room, checks_path=None, case="T2", variant="variant-1"):
        return export_bundle.export(room, case, variant, 1,
                                    T1_TASK if case == "T1" else T2_TASK,
                                    checks_path, "0" * 40, "completed", None)

    def probes_json(self, temp, repo):
        checks_path = Path(temp) / "checks.json"
        checks_path.write_text(json.dumps(probes.probe("T2", repo)))
        return checks_path

    # --- Finding 1: bind to the verified/accepted/probed candidate --------------------

    def test_positive_control_binds_and_scores_correct(self):
        # (f) the unmodified synthetic room binds every identity and stays correct.
        with tempfile.TemporaryDirectory() as temp:
            room, repo, _ = self.bound_room(Path(temp))
            bundle = self.export(room, self.probes_json(temp, repo))
            self.assertTrue(bundle["acceptance"]["bound_to_candidate"])
            self.assertIsNone(bundle["acceptance"]["unbound_reason"])
            self.assertTrue(bundle["verification"]["bound"])
            self.assertFalse(bundle["probe"]["stale"])
            self.assertIsNone(bundle["probe"]["stale_reason"])
            self.assertEqual(bundle["checks"]["accepted"], "pass")
            exported = score.score_bundle(bundle)
            self.assertIs(exported["accepted_correctness"], True)
            self.assertEqual(exported["elapsed_minutes"], 25.0)

    def test_regression_commit_after_acceptance_is_undetermined(self):
        # (a) HEAD moved after the accepted checkpoint: never a true verdict.
        with tempfile.TemporaryDirectory() as temp:
            room, repo, _ = self.bound_room(Path(temp))
            (Path(repo) / "summarize_cli.py").write_text("raise RuntimeError('unreviewed')\n")
            git(repo, "add", "-A")
            git(repo, "commit", "-q", "-m", "unreviewed regression")
            bundle = self.export(room, self.probes_json(temp, repo))
            self.assertFalse(bundle["acceptance"]["bound_to_candidate"])
            self.assertEqual(bundle["acceptance"]["unbound_reason"], "candidate_head_moved")
            self.assertEqual(bundle["checks"]["accepted"], "unavailable")
            self.assertIs(score.score_bundle(bundle)["accepted_correctness"], None)

    def test_dirty_candidate_is_undetermined(self):
        # (b) uncommitted drift on the accepted candidate leaves correctness unknown.
        with tempfile.TemporaryDirectory() as temp:
            room, repo, _ = self.bound_room(Path(temp))
            (Path(repo) / "summarize_cli.py").write_text("raise RuntimeError('dirty')\n")
            bundle = self.export(room, self.probes_json(temp, repo))
            self.assertFalse(bundle["acceptance"]["bound_to_candidate"])
            self.assertEqual(bundle["acceptance"]["unbound_reason"], "candidate_dirty")
            self.assertIs(score.score_bundle(bundle)["accepted_correctness"], None)

    def test_acceptance_digest_mismatch_is_undetermined(self):
        # (c) acceptance that names no passed verification for its candidate.
        with tempfile.TemporaryDirectory() as temp:
            room, repo, _ = self.bound_room(Path(temp))
            state = json.loads((room / "state.json").read_text())
            state["acceptances"][-1]["candidate_sha256"] = "2" * 64
            (room / "state.json").write_text(json.dumps(state))
            bundle = self.export(room, self.probes_json(temp, repo))
            self.assertFalse(bundle["acceptance"]["bound_to_candidate"])
            self.assertEqual(bundle["acceptance"]["unbound_reason"],
                             "no_passed_verification_for_accepted_candidate")
            self.assertIs(score.score_bundle(bundle)["accepted_correctness"], None)

    def test_stale_probe_output_is_marked_and_undetermined(self):
        # (d) a probe run before the last commit is stale, never silently merged.
        with tempfile.TemporaryDirectory() as temp:
            room, repo, _ = self.bound_room(Path(temp))
            checks_path = self.probes_json(temp, repo)
            (Path(repo) / "helper.py").write_text("# touched\n")
            git(repo, "add", "-A")
            git(repo, "commit", "-q", "-m", "post-probe commit")
            bundle = self.export(room, checks_path)
            self.assertTrue(bundle["probe"]["stale"])
            self.assertEqual(bundle["checks"]["gates_run_locally"], "unavailable")
            self.assertEqual(bundle["checks"]["probe_cli_stub_detected"], "unavailable")
            self.assertIs(score.score_bundle(bundle)["accepted_correctness"], None)

    def test_probe_on_different_bytes_same_head_is_stale_by_content(self):
        # A probe taken against different file bytes at the same HEAD is stale by
        # content: frozen-path results from those bytes are never merged.
        with tempfile.TemporaryDirectory() as temp:
            room, repo, _ = self.bound_room(Path(temp))
            fixture = PACKAGE / "tasks" / "T2-helper-only-proof" / "fixture" / "README.md"
            (Path(repo) / "README.md").write_text("# candidate-owned readme\n")
            git(repo, "add", "-A")
            head = git(repo, "commit", "-q", "-m", "candidate readme")
            state = json.loads((room / "state.json").read_text())
            checkpoint = json.loads((room / state["verifications"][0]["path"]).read_text())
            checkpoint["candidate"]["head"] = head
            (room / state["verifications"][0]["path"]).write_text(json.dumps(checkpoint))
            state["acceptances"][0]["evidence_sha256"] = hashlib.sha256(
                canonical(checkpoint)).hexdigest()
            (room / "state.json").write_text(json.dumps(state))
            committed = (Path(repo) / "README.md").read_bytes()
            (Path(repo) / "README.md").write_bytes(fixture.read_bytes())
            checks_path = self.probes_json(temp, repo)
            (Path(repo) / "README.md").write_bytes(committed)
            bundle = self.export(room, checks_path)
            self.assertTrue(bundle["probe"]["stale"])
            self.assertEqual(bundle["probe"]["stale_reason"], "content")
            self.assertEqual(bundle["checks"]["frozen_paths_unchanged"], "unavailable")
            self.assertIs(score.score_bundle(bundle)["accepted_correctness"], None)

    def test_probe_on_a_dirty_candidate_is_stale_dirty(self):
        # A probe taken while the worktree is dirty, still dirty at export, is stale.
        with tempfile.TemporaryDirectory() as temp:
            room, repo, _ = self.bound_room(Path(temp))
            (Path(repo) / "helper.py").write_text("# uncommitted\n")
            checks_path = self.probes_json(temp, repo)
            bundle = self.export(room, checks_path)
            self.assertTrue(bundle["probe"]["stale"])
            self.assertEqual(bundle["probe"]["stale_reason"], "dirty")
            self.assertEqual(bundle["checks"]["frozen_paths_unchanged"], "unavailable")

    def test_failing_control_makes_stub_probe_unavailable(self):
        # (e) a test module that fails on the unmodified copy cannot detect a stub.
        with tempfile.TemporaryDirectory() as temp:
            room, repo, _ = self.bound_room(Path(temp))
            test_file = Path(repo) / "tests" / "test_summarize_cli.py"
            test_file.write_text("import sys\nsys.exit(1)\n")
            result = probes.probe("T2", repo)
            self.assertEqual(result["checks"]["probe_cli_stub_detected"], "unavailable")
            self.assertEqual(result["details"]["stub_control"]["return_code"], 1)

    def test_invented_owned_file_is_a_failure(self):
        # (g) an untracked owned path is an invented contract: fail, not unavailable.
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            fixture = PACKAGE / "tasks" / "T1-incomplete-handoff" / "fixture"
            repo = temp / "t1-candidate"
            dry_run.shutil.copytree(fixture, repo)
            for args in (("init", "-q"), ("config", "user.name", "T"),
                         ("config", "user.email", "t@example.invalid"), ("add", "-A")):
                subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
            baseline = git(repo, "commit", "-q", "-m", "baseline")
            room, _, _ = self.bound_room(temp / "roomside")
            (room / "handoffs" / "h.json").write_text(
                json.dumps({"worktree": str(repo), "baseline_commit": baseline}))
            report = {"outcome": "changes_required", "implementation_complete": False,
                      "changes": [], "tests_reported": [], "review_findings": [],
                      "remaining_gaps": ["schema missing"], "backlog": [], "routing_log": [],
                      "spec_revision": 1, "spec_sha256": "0" * 64, "baseline_commit": baseline}
            receipt = json.loads((room / "receipts" / "implementation-1").glob("*.json").__next__().read_text())
            receipt["messages"][-1]["text"] = json.dumps(report)
            (room / "receipts" / "implementation-1").glob("*.json").__next__().write_text(json.dumps(receipt))
            clean = self.export(room, None, case="T1")
            self.assertEqual(clean["checks"]["no_invented_contract"], "pass")
            (repo / "report_cli.py").write_text("# invented\n")
            bundle = self.export(room, None, case="T1")
            self.assertEqual(bundle["checks"]["no_invented_contract"], "fail")

    def test_bundle_without_binding_fields_scores_undetermined(self):
        # An additive schema: a bundle that lacks the binding fields stays undetermined.
        with tempfile.TemporaryDirectory() as temp:
            room, repo, _ = self.bound_room(Path(temp))
            bundle = self.export(room, self.probes_json(temp, repo))
            bundle["acceptance"].pop("bound_to_candidate")
            bundle["acceptance"].pop("unbound_reason")
            bundle["verification"].pop("bound")
            self.assertIs(score.score_bundle(bundle)["accepted_correctness"], None)

    # --- Finding 2: dedupe launches across observations --------------------------------

    def _two_observation_room(self, temp, contradict=False, pin_first=False):
        room, repo, _ = self.bound_room(temp)
        directory = room / "outcomes" / "implementation-1"
        state = json.loads((room / "state.json").read_text())
        request = state["requests"]["implementation-1"]
        base = request["created_at"]
        launch1 = {"agent_id": "a1", "tool_use_id": "t1", "role": "pr-sonnet",
                   "model": "claude-sonnet-5", "outcome": "qualified",
                   "interval": {"launch": base + 7 * 60, "end": base + 14 * 60}}
        launch2 = {**launch1, "agent_id": "a2", "tool_use_id": "t2", "model": "claude-other",
                   "outcome": "contradicted",
                   "interval": {"launch": base + 15 * 60, "end": base + 17 * 60}}
        first_outcome = "unresolved" if contradict else "qualified"
        (directory / "o1.json").write_text(json.dumps(
            {"worker_observations": {"coverage": "partial", "recorded_at": base,
                                     "executions": [], "launches": [{**launch1, "outcome": first_outcome}]}}))
        (directory / "o.json").rename(directory / "o2.json")
        (directory / "o2.json").write_text(json.dumps(
            {"worker_observations": {"coverage": "complete", "recorded_at": base + 60,
                                     "executions": [launch1], "launches": [launch1, launch2]}}))
        if pin_first:
            request["semantic_outcome_sha256"] = "o1"
        (room / "state.json").write_text(json.dumps(state))
        return room, repo

    def test_worker_launches_dedupe_across_observations(self):
        # Two observations of the same two launches count exactly two launches.
        with tempfile.TemporaryDirectory() as temp:
            room, _ = self._two_observation_room(Path(temp))
            bundle = self.export(room)
            request = bundle["requests"][1]
            self.assertEqual(request["worker_observations"], 2)
            launches = {launch["outcome"]: launch for launch in request["worker_launches"]}
            self.assertEqual(len(launches), 2)
            self.assertEqual(launches["qualified"]["observations"], 2)
            self.assertEqual(launches["contradicted"]["observations"], 1)
            exported = score.score_bundle(bundle)
            self.assertEqual(exported["usage"]["worker"]["unknown_items"], 2)

    def test_contradicting_observations_preserve_both_values(self):
        # The current observation wins the label; the contradiction is recorded.
        with tempfile.TemporaryDirectory() as temp:
            room, _ = self._two_observation_room(Path(temp), contradict=True)
            launch = self.export(room)["requests"][1]["worker_launches"][0]
            self.assertEqual(launch["outcome"], "qualified")
            self.assertEqual(launch["contradictions"],
                             [{"field": "outcome", "values": ["qualified", "unresolved"]}])

    def test_semantic_pointer_selects_the_named_observation(self):
        # semantic_outcome_sha256 pins the current observation explicitly.
        with tempfile.TemporaryDirectory() as temp:
            room, _ = self._two_observation_room(Path(temp), contradict=True, pin_first=True)
            launch = self.export(room)["requests"][1]["worker_launches"][0]
            self.assertEqual(launch["outcome"], "unresolved")

    def test_agent_id_known_later_merges_into_one_launch(self):
        # An initially unresolved child whose agent_id becomes known is one launch.
        with tempfile.TemporaryDirectory() as temp:
            room, _, _ = self.bound_room(Path(temp))
            directory = room / "outcomes" / "implementation-1"
            request = self._request_state(room)["implementation-1"]
            base = request["created_at"]
            launch = {"agent_id": "a1", "tool_use_id": "t1", "role": "pr-sonnet",
                      "model": "claude-sonnet-5", "outcome": "qualified",
                      "interval": {"launch": base + 7 * 60, "end": base + 14 * 60}}
            earlier = {"worker_observations": {
                "coverage": "partial", "recorded_at": base, "executions": [],
                "launches": [{**launch, "agent_id": None, "outcome": "unresolved"}]}}
            later = {"worker_observations": {
                "coverage": "complete", "recorded_at": base + 60, "executions": [launch],
                "launches": [launch, {**launch, "agent_id": "a2", "tool_use_id": "t2",
                                      "model": "claude-other", "outcome": "contradicted",
                                      "interval": {"launch": base + 15 * 60, "end": base + 17 * 60}}]}}
            (directory / "o.json").unlink()
            (directory / "a.json").write_text(json.dumps(earlier))
            (directory / "b.json").write_text(json.dumps(later))
            launches = self.export(room)["requests"][1]["worker_launches"]
            merged = [launch for launch in launches
                      if launch["observed_child_model"] == "claude-sonnet-5"]
            self.assertEqual(len(launches), 2)
            self.assertEqual(len(merged), 1)
            self.assertEqual(merged[0]["agent_ids"], ["a1"])
            self.assertEqual(merged[0]["observations"], 2)
            self.assertEqual(merged[0]["outcome"], "qualified")
            self.assertNotIn("agent_id", [c["field"] for c in merged[0].get("contradictions", [])])

    def test_launches_without_tool_use_id_stay_separate(self):
        # Launches with no usable tool-use id never merge under a null key.
        with tempfile.TemporaryDirectory() as temp:
            room, _, _ = self.bound_room(Path(temp))
            directory = room / "outcomes" / "implementation-1"
            request = self._request_state(room)["implementation-1"]
            base = request["created_at"]
            launches = [{"agent_id": "a1", "tool_use_id": None, "role": "pr-sonnet",
                         "model": "claude-sonnet-5", "outcome": "qualified",
                         "interval": {"launch": base + 7 * 60, "end": base + 9 * 60}},
                        {"agent_id": "a2", "tool_use_id": None, "role": "pr-opus",
                         "model": "claude-opus-5", "outcome": "qualified",
                         "interval": {"launch": base + 10 * 60, "end": base + 12 * 60}}]
            (directory / "o.json").unlink()
            (directory / "a.json").write_text(json.dumps(
                {"worker_observations": {"coverage": "complete", "recorded_at": base,
                                         "executions": [], "launches": launches}}))
            exported = self.export(room)["requests"][1]["worker_launches"]
            self.assertEqual(len(exported), 2)

    def _request_state(self, room):
        return json.loads((room / "state.json").read_text())["requests"]

    # --- Finding 3: a receipt is not proof of settlement --------------------------------

    def test_uncertain_request_with_receipt_stays_unknown(self):
        # An uncertain request keeps elapsed unknown even with an intact receipt.
        with tempfile.TemporaryDirectory() as temp:
            room, repo, _ = self.bound_room(Path(temp))
            state = json.loads((room / "state.json").read_text())
            state["requests"]["implementation-1"]["state"] = "uncertain"
            (room / "state.json").write_text(json.dumps(state))
            bundle = self.export(room)
            request = bundle["requests"][1]
            self.assertFalse(request["settled"])
            self.assertIsNone(bundle["elapsed_minutes"])

    def test_missing_review_receipt_stays_unknown(self):
        # The existing no-receipt case keeps its unknown elapsed.
        with tempfile.TemporaryDirectory() as temp:
            room, repo, _ = self.bound_room(Path(temp), settled_review=False)
            bundle = self.export(room)
            self.assertIsNone(bundle["elapsed_minutes"])


if __name__ == "__main__":
    unittest.main()
