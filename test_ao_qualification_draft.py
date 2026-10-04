import copy
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

import ao_engineering_model
import ao_model_qualification
import ao_qualification_draft
from room import RoomError


NOW = "2026-10-01T00:00:00Z"
SOURCE_ID = "official-model-configuration"
CURRENT_IDS = {"claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5"}


class QualificationDraftTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "ao"
        self.root.mkdir(mode=0o700)
        self.claude_bin = self.base / "configured-claude"
        self.write_executable(b"claude-fable-5-1 claude-opus-5-5 claude-sonnet-5\n")
        (self.base / "config.json").write_text(
            json.dumps({"claude_bin": str(self.claude_bin)}), encoding="utf-8")
        self.evidence = (b"Synthetic official model page: claude-fable-5-1 claude-opus-5-5 "
                         b"claude-sonnet-5\n")
        self.evidence_file = self.base / "evidence.bin"
        self.evidence_file.write_bytes(self.evidence)
        self.evidence_file.chmod(0o600)
        self.artifact = {
            "format": ao_model_qualification.FORMAT,
            "revision": 1,
            "qualified_at": "2026-06-02T00:00:00Z",
            "scope": copy.deepcopy(ao_model_qualification.SCOPE),
            "families": {
                "fable": {"expected_model": "claude-fable-5-1", "source_ids": [SOURCE_ID]},
                "opus": {"expected_model": "claude-opus-5-5", "source_ids": [SOURCE_ID],
                         "minimum_claude_code_version": "2.1.280"},
                "sonnet": {"expected_model": "claude-sonnet-5", "source_ids": [SOURCE_ID]},
            },
            "sources": [{
                "id": SOURCE_ID,
                "uri": "https://docs.claude.com/en/model-configuration",
                "captured_at": "2026-06-01T00:00:00Z",
                "sha256": hashlib.sha256(self.evidence).hexdigest(),
                "evidence_file": str(self.evidence_file),
            }],
        }
        self.artifact_path = self.base / "qualification.json"
        self.artifact_path.write_text(json.dumps(self.artifact, indent=2, sort_keys=True),
                                      encoding="utf-8")
        self.artifact_path.chmod(0o600)
        self.pointer = {"path": str(self.artifact_path),
                        "sha256": ao_model_qualification.digest(self.artifact)}
        (self.root / "config.json").write_text(
            json.dumps({"family_qualification": self.pointer}), encoding="utf-8")
        self.bodies = {SOURCE_ID: self.evidence}

    def write_executable(self, ids_bytes):
        self.claude_bin.write_bytes(b"#!/bin/sh\n# filler\n" + ids_bytes)
        self.claude_bin.chmod(0o700)

    def executable_ids(self):
        return ao_qualification_draft.embedded_model_ids(str(self.claude_bin))

    def draft_dir(self):
        return self.root / ao_qualification_draft.DRAFTS

    # --- version_tuple / dated -------------------------------------------------

    def test_version_tuple_orders_components_numerically(self):
        self.assertLess(ao_qualification_draft.version_tuple("claude-opus-5"),
                        ao_qualification_draft.version_tuple("claude-opus-5-5"))
        self.assertLess(ao_qualification_draft.version_tuple("claude-opus-9"),
                        ao_qualification_draft.version_tuple("claude-opus-10"))
        self.assertEqual(ao_qualification_draft.version_tuple("claude-fable-5-1"), (5, 1))

    def test_dated_identifiers(self):
        self.assertTrue(ao_qualification_draft.dated("claude-sonnet-4-5-20250929"))
        self.assertFalse(ao_qualification_draft.dated("claude-fable-5-1"))

    # --- embedded_model_ids ----------------------------------------------------

    def test_embedded_model_ids_spans_chunk_boundary(self):
        path = self.base / "chunked-executable"
        path.write_bytes(b"f" * 250 + b"claude-fable-5-5"
                         + b" tail text claude-gpt-9 claude-9 more filler\n")
        path.chmod(0o700)
        found = ao_qualification_draft.embedded_model_ids(str(path), chunk_bytes=256)
        self.assertEqual(found, {"claude-fable-5-5"})

    def test_embedded_model_ids_ignores_identifier_prefix_at_chunk_end(self):
        path = self.base / "prefix-at-boundary"
        path.write_bytes(b"f" * 242 + b"claude-fable-5-5" + b" filler text\n")
        path.chmod(0o700)
        self.assertEqual(
            ao_qualification_draft.embedded_model_ids(str(path), chunk_bytes=256),
            {"claude-fable-5-5"})

    def test_embedded_model_ids_refuses_missing_and_non_regular(self):
        with self.assertRaises(RoomError):
            ao_qualification_draft.embedded_model_ids(str(self.base / "absent"))
        with self.assertRaises(RoomError):
            ao_qualification_draft.embedded_model_ids(str(self.base / "ao"))
        link = self.base / "linked-claude"
        link.symlink_to(self.claude_bin)
        with self.assertRaises(RoomError):
            ao_qualification_draft.embedded_model_ids(str(link))

    def test_embedded_model_ids_refuses_a_file_growing_mid_scan(self):
        path = self.base / "growing-executable"
        path.write_bytes(b"filler " + b"x" * 200 + b" claude-fable-5-5\n")
        path.chmod(0o700)
        real_fdopen = os.fdopen

        class MidScanGrowth:
            """A read wrapper whose first chunk read grows the file via a second handle."""
            def __init__(self, stream):
                self.stream = stream
                self.reads = 0

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.stream.close()
                return False

            def read(self, size=-1):
                self.reads += 1
                if self.reads == 1:
                    with path.open("ab") as other:
                        other.write(b"and more claude-opus-9\n")
                return self.stream.read(size)

        def fdopen(descriptor, mode, closefd=False):
            return MidScanGrowth(real_fdopen(descriptor, mode, closefd=False))

        with patch.object(ao_qualification_draft.os, "fdopen", fdopen):
            with self.assertRaisesRegex(RoomError, "changed during the scan"):
                ao_qualification_draft.embedded_model_ids(str(path), chunk_bytes=64)

    # --- proposals -------------------------------------------------------------

    def test_no_newer_identifier_proposes_nothing(self):
        rows = ao_qualification_draft.proposals(self.artifact, self.bodies, set(CURRENT_IDS),
                                                (2, 1, 285))
        self.assertEqual([row["to"] for row in rows], [None, None, None])
        self.assertEqual({row["reason"] for row in rows}, {"none_newer"})
        self.assertIsNone(ao_qualification_draft.draft(
            self.root, self.artifact, self.pointer, self.bodies, set(CURRENT_IDS), (2, 1, 285), NOW))
        self.assertFalse(self.draft_dir().exists())

    def test_newer_fable_identifier_drafts_revision_two(self):
        body = self.evidence + b"now serving claude-fable-5-5\n"
        bodies = {SOURCE_ID: body}
        executable_ids = set(CURRENT_IDS) | {"claude-fable-5-5"}
        rows = ao_qualification_draft.proposals(self.artifact, bodies, executable_ids,
                                                (2, 1, 285))
        by_family = {row["family"]: row for row in rows}
        self.assertEqual(by_family["fable"]["to"], "claude-fable-5-5")
        self.assertEqual(by_family["fable"]["floor"], "2.1.285")
        self.assertIsNone(by_family["fable"]["reason"])
        self.assertEqual(by_family["fable"]["from"], "claude-fable-5-1")
        self.assertIsNone(by_family["opus"]["to"])
        result = ao_qualification_draft.draft(self.root, self.artifact, self.pointer, bodies,
                                              executable_ids, (2, 1, 285), NOW)
        self.assertIsNotNone(result)
        draft_dir = Path(result["draft_dir"])
        self.assertEqual(draft_dir.parent, self.root / ao_qualification_draft.DRAFTS)
        draft_artifact = json.loads((draft_dir / "artifact.json").read_bytes())
        ao_model_qualification.verify_sources(draft_artifact)
        self.assertEqual(draft_artifact["revision"], 2)
        self.assertEqual(draft_artifact["qualified_at"], NOW)
        self.assertEqual(draft_artifact["families"]["fable"]["expected_model"],
                         "claude-fable-5-5")
        self.assertEqual(draft_artifact["families"]["fable"]["minimum_claude_code_version"],
                         "2.1.285")
        for family in ("opus", "sonnet"):
            self.assertEqual(draft_artifact["families"][family],
                             self.artifact["families"][family])
        descriptor = draft_artifact["sources"][0]
        self.assertEqual(descriptor["id"], SOURCE_ID)
        self.assertEqual(descriptor["sha256"], hashlib.sha256(body).hexdigest())
        self.assertEqual(Path(descriptor["evidence_file"]).read_bytes(), body)
        additions = json.loads((draft_dir / "engineering_models.json").read_bytes())
        self.assertEqual(additions, {"claude-fable-5-5": {
            "harness": "claude-code", "reasoning_effort": "max",
            "minimum_claude_code_version": "2.1.285"}})
        proposal = json.loads((draft_dir / "proposal.json").read_bytes())
        self.assertEqual(proposal["revision"], 2)
        self.assertEqual(proposal["previous"], self.pointer)
        self.assertEqual(proposal["artifact_sha256"],
                         ao_model_qualification.digest(draft_artifact))
        self.assertEqual(proposal["artifact_sha256"], result["artifact_sha256"])
        self.assertIn("adopt --draft", result["adopt_command"])

        listing = sorted(str(path.relative_to(draft_dir)) for path in draft_dir.rglob("*"))
        again = ao_qualification_draft.draft(self.root, self.artifact, self.pointer, bodies,
                                             executable_ids, (2, 1, 285), NOW)
        self.assertEqual(again["draft_dir"], result["draft_dir"])
        self.assertEqual(again["artifact_sha256"], result["artifact_sha256"])
        self.assertEqual(sorted(str(path.relative_to(draft_dir))
                                for path in draft_dir.rglob("*")), listing)
        # Same proposal with different fresh bytes reuses the retained, self-verifying capture.
        drifted = {SOURCE_ID: body + b"updated wording, still claude-fable-5-5\n"}
        later = ao_qualification_draft.draft(self.root, self.artifact, self.pointer, drifted,
                                             executable_ids, (2, 1, 285), "2026-10-02T00:00:00Z")
        self.assertEqual(later["draft_dir"], result["draft_dir"])
        self.assertEqual(later["artifact_sha256"], result["artifact_sha256"])
        self.assertEqual(sorted(str(path.relative_to(draft_dir))
                                for path in draft_dir.rglob("*")), listing)

    def test_draft_refuses_a_partial_directory_and_publishes_without_staging_leftovers(self):
        body = self.evidence + b"now serving claude-fable-5-5\n"
        bodies = {SOURCE_ID: body}
        executable_ids = set(CURRENT_IDS) | {"claude-fable-5-5"}
        rows = ao_qualification_draft.proposals(self.artifact, bodies, executable_ids, (2, 1, 285))
        name = "r2-%s" % ao_model_qualification.digest(
            ao_qualification_draft._proposal_key(rows))[:12]
        partial = self.draft_dir() / name
        (partial / "evidence").mkdir(parents=True)  # an interrupted publish, never completed
        (partial / "evidence" / (SOURCE_ID + ".bin")).write_bytes(body)
        with self.assertRaisesRegex(RoomError, "already exists with other bytes"):
            ao_qualification_draft.draft(self.root, self.artifact, self.pointer, bodies,
                                         executable_ids, (2, 1, 285), NOW)
        shutil.rmtree(partial)
        result = ao_qualification_draft.draft(self.root, self.artifact, self.pointer, bodies,
                                              executable_ids, (2, 1, 285), NOW)
        self.assertIsNotNone(result)
        self.assertTrue((Path(result["draft_dir"]) / "proposal.json").is_file())
        self.assertFalse((self.draft_dir() / "staging").exists())

    def test_adopt_command_quotes_private_paths_for_the_shell(self):
        body = self.evidence + b"now serving claude-fable-5-5\n"
        spaced = self.base / "home with space$and$dollar"
        root = spaced / "ao"
        root.mkdir(parents=True, mode=0o700)
        result = ao_qualification_draft.draft(root, self.artifact, self.pointer,
                                              {SOURCE_ID: body}, set(CURRENT_IDS) | {"claude-fable-5-5"},
                                              (2, 1, 285), NOW)
        parts = shlex.split(result["adopt_command"])
        self.assertEqual(parts[:3], ["python3", "ao_qualification_draft.py", "--home"])
        self.assertEqual(parts[3], str(spaced))
        self.assertEqual(parts[4:6], ["adopt", "--draft"])
        self.assertEqual(parts[6], result["draft_dir"])
        self.assertEqual(parts[7:], ["--authorization", "<operator authorization>"])
        self.assertIn("<operator authorization>", result["adopt_command"])

    def test_candidate_must_appear_in_source_and_executable(self):
        newer_in_source = self.evidence + b"claude-fable-5-5\n"
        rows = ao_qualification_draft.proposals(
            self.artifact, {SOURCE_ID: newer_in_source}, set(CURRENT_IDS), (2, 1, 285))
        self.assertEqual({row["family"]: row["reason"] for row in rows}["fable"],
                         "not_embedded_in_executable")
        rows = ao_qualification_draft.proposals(
            self.artifact, self.bodies, set(CURRENT_IDS) | {"claude-fable-5-5"}, (2, 1, 285))
        self.assertEqual({row["family"]: row["reason"] for row in rows}["fable"],
                         "not_in_source_capture")

    def test_older_and_dated_identifiers_are_never_proposals(self):
        older = {SOURCE_ID: b"page mentions claude-fable-5-1 and claude-fable-5\n"}
        rows = ao_qualification_draft.proposals(self.artifact, older,
                                                {"claude-fable-5", "claude-fable-5-1"}, (2, 1, 285))
        self.assertEqual({row["family"]: row["reason"] for row in rows}["fable"], "none_newer")
        dated_body = {SOURCE_ID: self.evidence + b"claude-fable-5-5-20260101\n"}
        rows = ao_qualification_draft.proposals(
            self.artifact, dated_body, set(CURRENT_IDS) | {"claude-fable-5-5-20260101"},
            (2, 1, 285))
        self.assertEqual({row["family"]: row["reason"] for row in rows}["fable"], "none_newer")

    def test_unavailable_source_blocks_the_family_and_the_draft(self):
        bodies = {SOURCE_ID: None}
        rows = ao_qualification_draft.proposals(
            self.artifact, bodies, set(CURRENT_IDS) | {"claude-fable-5-5"}, (2, 1, 285))
        self.assertEqual({row["reason"] for row in rows}, {"source_unavailable"})
        self.assertIsNone(ao_qualification_draft.draft(
            self.root, self.artifact, self.pointer, bodies,
            set(CURRENT_IDS) | {"claude-fable-5-5"}, (2, 1, 285), NOW))
        self.assertFalse(self.draft_dir().exists())

    def test_executable_below_family_floor_proposes_nothing(self):
        body = self.evidence + b"claude-opus-6\n"
        executable_ids = set(CURRENT_IDS) | {"claude-opus-6"}
        rows = ao_qualification_draft.proposals(self.artifact, {SOURCE_ID: body},
                                                executable_ids, (2, 1, 270))
        by_family = {row["family"]: row for row in rows}
        self.assertIsNone(by_family["opus"]["to"])
        self.assertEqual(by_family["opus"]["reason"], "executable_below_family_floor")

    def test_a_source_capture_dropping_a_qualified_model_blocks_the_draft(self):
        # The fresh capture adds claude-fable-5-5 but no longer names the qualified opus at all.
        body = b"Synthetic official model page: claude-fable-5-5 claude-sonnet-5\n"
        bodies = {SOURCE_ID: body}
        executable_ids = set(CURRENT_IDS) | {"claude-fable-5-5"}
        rows = ao_qualification_draft.proposals(self.artifact, bodies, executable_ids, (2, 1, 285))
        by_family = {row["family"]: row for row in rows}
        self.assertEqual(by_family["fable"]["to"], "claude-fable-5-5")
        self.assertEqual(by_family["opus"]["reason"], "current_model_not_in_source_capture")
        self.assertEqual(by_family["sonnet"]["reason"], "none_newer")
        self.assertIsNone(ao_qualification_draft.draft(
            self.root, self.artifact, self.pointer, bodies, executable_ids, (2, 1, 285), NOW))
        self.assertFalse(self.draft_dir().exists())

    # --- adopt -----------------------------------------------------------------

    def _drafted(self, authorization_setup=True):
        body = self.evidence + b"now serving claude-fable-5-5\n"
        bodies = {SOURCE_ID: body}
        self.write_executable(b"claude-fable-5-1 claude-opus-5-5 claude-sonnet-5 "
                              b"claude-fable-5-5\n")
        executable_ids = self.executable_ids()
        result = ao_qualification_draft.draft(self.root, self.artifact, self.pointer, bodies,
                                              executable_ids, (2, 1, 285), NOW)
        self.assertIsNotNone(result)
        return result, executable_ids

    def test_adopt_switches_pointer_merges_models_and_records(self):
        result, executable_ids = self._drafted()
        before = (self.root / "config.json").read_bytes()
        adopted = ao_qualification_draft.adopt(self.root, result["draft_dir"],
                                               "operator approves fable-5-5 adoption",
                                               executable_ids, (2, 1, 285))
        self.assertTrue(adopted["adopted"])
        config = json.loads((self.root / "config.json").read_bytes())
        self.assertEqual(config["family_qualification"],
                         {"path": str(Path(result["draft_dir"]) / "artifact.json"),
                          "sha256": result["artifact_sha256"]})
        self.assertEqual(config["engineering_models"]["claude-fable-5-5"],
                         {"harness": "claude-code", "reasoning_effort": "max",
                          "minimum_claude_code_version": "2.1.285"})
        backup = Path(adopted["config_backup"])
        self.assertTrue(backup.name.startswith("config.json.bak-"))
        self.assertTrue(backup.name.endswith("-r%d-%s" % (adopted["revision"],
                                                         result["artifact_sha256"][:12])))
        self.assertEqual(backup.read_bytes(), before)
        record = json.loads(Path(adopted["record"]).read_bytes())
        self.assertEqual(record["authorization"], "operator approves fable-5-5 adoption")
        self.assertEqual(record["previous_pointer"], self.pointer)
        self.assertEqual(record["pointer"], config["family_qualification"])
        self.assertEqual(record["engineering_models_added"], ["claude-fable-5-5"])
        self.assertEqual(record["config_backup"], str(backup))
        self.assertNotIn("recovered", record)
        policy, _ = ao_engineering_model.effective_policy(self.root)
        self.assertIn("claude-fable-5-5", policy["models"])
        self.assertEqual(policy["families"]["fable"]["execution_qualification"]["expected_model"],
                         "claude-fable-5-5")
        again = ao_qualification_draft.adopt(self.root, result["draft_dir"],
                                             "operator approves fable-5-5 adoption",
                                             executable_ids, (2, 1, 285))
        self.assertTrue(again["already_adopted"])
        self.assertFalse(again["adopted"])

    def test_adopt_refuses_stale_previous_pointer(self):
        result, executable_ids = self._drafted()
        config = json.loads((self.root / "config.json").read_bytes())
        config["family_qualification"] = {"path": self.pointer["path"], "sha256": "0" * 64}
        (self.root / "config.json").write_text(json.dumps(config), encoding="utf-8")
        with self.assertRaises(RoomError):
            ao_qualification_draft.adopt(self.root, result["draft_dir"], "authorized",
                                         executable_ids, (2, 1, 285))

    def test_adopt_refuses_identifier_no_longer_embedded(self):
        result, _ = self._drafted()
        with self.assertRaises(RoomError):
            ao_qualification_draft.adopt(self.root, result["draft_dir"], "authorized",
                                         set(CURRENT_IDS), (2, 1, 285))

    def test_adopt_refuses_an_unknown_or_below_floor_executable_version(self):
        result, executable_ids = self._drafted()
        with self.assertRaisesRegex(RoomError, "version is unknown"):
            ao_qualification_draft.adopt(self.root, result["draft_dir"], "authorized",
                                         executable_ids, None)
        with self.assertRaisesRegex(RoomError, "below the proposed floor"):
            ao_qualification_draft.adopt(self.root, result["draft_dir"], "authorized",
                                         executable_ids, (2, 1, 284))
        config = json.loads((self.root / "config.json").read_bytes())
        self.assertEqual(config["family_qualification"], self.pointer)
        self.assertFalse((self.root / ao_qualification_draft.ADOPTIONS).exists())

    def test_adopt_refuses_addition_colliding_with_bundled_model(self):
        result, executable_ids = self._drafted()
        additions_path = Path(result["draft_dir"]) / "engineering_models.json"
        additions_path.unlink()
        additions_path.write_text(json.dumps({
            "claude-fable-5-5": {"harness": "claude-code", "reasoning_effort": "max",
                                 "minimum_claude_code_version": "2.1.285"},
            "claude-opus-5-5": {"harness": "claude-code", "reasoning_effort": "max",
                                "minimum_claude_code_version": "9.9.9"}}))
        additions_path.chmod(0o600)
        with self.assertRaises(RoomError):
            ao_qualification_draft.adopt(self.root, result["draft_dir"], "authorized",
                                         executable_ids, (2, 1, 285))

    def test_adopt_refuses_empty_authorization_and_outside_path(self):
        result, executable_ids = self._drafted()
        with self.assertRaises(RoomError):
            ao_qualification_draft.adopt(self.root, result["draft_dir"], "", executable_ids, (2, 1, 285))
        with self.assertRaises(RoomError):
            ao_qualification_draft.adopt(self.root, result["draft_dir"], "   ", executable_ids, (2, 1, 285))
        with self.assertRaises(RoomError):
            ao_qualification_draft.adopt(self.root, str(self.base / "elsewhere"), "authorized",
                                         executable_ids, (2, 1, 285))
        with self.assertRaises(RoomError):
            ao_qualification_draft.adopt(self.root, str(Path(result["draft_dir"]).parent),
                                         "authorized", executable_ids, (2, 1, 285))

    def test_adopt_completes_an_adoption_record_lost_after_the_switch(self):
        result, executable_ids = self._drafted()
        failed = []
        real_store_once = ao_engineering_model.store_once

        def dropping_store_once(directory, relative, value):
            if relative.startswith(ao_qualification_draft.ADOPTIONS) and not failed:
                failed.append(relative)
                raise OSError("synthetic record write loss")
            return real_store_once(directory, relative, value)

        with patch.object(ao_engineering_model, "store_once", dropping_store_once):
            with self.assertRaises(OSError):
                ao_qualification_draft.adopt(self.root, result["draft_dir"], "authorized",
                                             executable_ids, (2, 1, 285))
        config = json.loads((self.root / "config.json").read_bytes())
        self.assertEqual(config["family_qualification"]["sha256"], result["artifact_sha256"])
        adoptions = self.root / ao_qualification_draft.ADOPTIONS
        self.assertEqual(list(adoptions.glob("*.json")) if adoptions.exists() else [], [])
        recovered = ao_qualification_draft.adopt(self.root, result["draft_dir"], "authorized",
                                                 executable_ids, (2, 1, 285))
        self.assertTrue(recovered["adopted"])
        self.assertTrue(recovered["recovered"])
        self.assertEqual(recovered["revision"], result["revision"])
        record = json.loads(Path(recovered["record"]).read_bytes())
        self.assertIs(record["recovered"], True)
        self.assertEqual(record["pointer"]["sha256"], result["artifact_sha256"])
        self.assertEqual(record["previous_pointer"], self.pointer)
        self.assertEqual(record["authorization"], "authorized")
        self.assertTrue(str(record["config_backup"]).endswith(
            "-r%d-%s" % (result["revision"], result["artifact_sha256"][:12])))
        settled = ao_qualification_draft.adopt(self.root, result["draft_dir"], "authorized",
                                               executable_ids, (2, 1, 285))
        self.assertTrue(settled["already_adopted"])

    def test_adopt_proves_the_candidate_policy_before_switching(self):
        result, executable_ids = self._drafted()
        before = (self.root / "config.json").read_bytes()
        calls = []

        def failing_policy(candidate_root):
            calls.append(Path(candidate_root))
            raise RoomError("synthetic candidate policy failure")

        with patch.object(ao_engineering_model, "effective_policy", failing_policy):
            with self.assertRaisesRegex(RoomError, "effective-policy"):
                ao_qualification_draft.adopt(self.root, result["draft_dir"], "authorized",
                                             executable_ids, (2, 1, 285))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].parent, self.root)
        self.assertTrue(calls[0].name.startswith("adopt-validate-"))
        self.assertFalse(calls[0].exists())  # the validation root is removed again
        self.assertEqual((self.root / "config.json").read_bytes(), before)  # nothing was switched
        self.assertFalse((self.root / ao_qualification_draft.ADOPTIONS).exists())
        backups = list(self.root.glob("config.json.bak-*"))
        self.assertEqual(len(backups), 1)  # only the create-once backup file remains
        self.assertEqual(backups[0].read_bytes(), before)

    # --- CLI -------------------------------------------------------------------

    def test_cli_draft_never_fetches_a_disallowed_source_host(self):
        import ao_release_check
        artifact = copy.deepcopy(self.artifact)
        artifact["sources"][0]["uri"] = "https://example.invalid/model-table"
        self.artifact_path.write_text(json.dumps(artifact, indent=2, sort_keys=True),
                                      encoding="utf-8")
        pointer = {"path": str(self.artifact_path),
                   "sha256": ao_model_qualification.digest(artifact)}
        (self.root / "config.json").write_text(
            json.dumps({"family_qualification": pointer}), encoding="utf-8")
        fetch = Mock(side_effect=AssertionError("a disallowed source is never fetched"))
        with patch.object(ao_release_check, "_fetch_source", fetch):
            result = ao_qualification_draft._draft_command(self.root, str(self.claude_bin))
        fetch.assert_not_called()
        self.assertEqual(result["outcome"], "none")
        self.assertEqual({row["reason"] for row in result["rows"]}, {"source_unavailable"})


if __name__ == "__main__":
    unittest.main()
