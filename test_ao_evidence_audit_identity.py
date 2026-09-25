"""Independent offline identity/history and descriptor-I/O regressions for #58.

All paths, SQLite rows and JSONL records are temporary synthetic fixtures. Historical
fixtures follow the retained producer shapes and invoke only their pure record builders;
the audit is never allowed to call live provider/adoption validators.
"""

import copy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import ao_evidence_audit as audit
import ao_evidence_audit_io as fs
import ao_evidence_audit_native as native
import ao_prompt_metrics as metrics
import ao_provider_transition as transition
import ao_routing_adoption as adoption
from test_ao_evidence_audit import AuditFixture, NATIVE_UUID, canon, completed_result


class HistoryFixture(AuditFixture):
    """The real source/target/adoption record relationships, with synthetic content only."""

    def put(self, relative, value):
        path = self.home / "ao" / "rooms" / self.room / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        data = (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode()
        path.write_bytes(data)
        return hashlib.sha256(data).hexdigest()

    def history(self, *, adopt=False):
        self.build(routing_version=1 if adopt else 2)
        old = copy.deepcopy(self.prepared)
        old["server"] = {"command": "/synthetic/python", "args": ["old-adapter"],
                         "env": {"PROJECT_ROOM_WORKTREE": str(self.workspace)}}
        old_sha = canon(old)
        self.put("preparation.json", old)
        self.state["preparation_sha256"] = old_sha
        fields = ("session_id", "harness", "model", "reasoning_effort", "conversation_id", "branch_id")
        self.state["bindings"] = {"engineer": {key: self.request[key] for key in fields},
                                  "reviewer": {"session_id": "synthetic-reviewer", "harness": "codex",
                                               "model": "synthetic-astra", "reasoning_effort": "high",
                                               "conversation_id": "review-conversation", "branch_id": "review-branch"}}
        before = copy.deepcopy(self.state)
        new_delegate = {**copy.deepcopy(self.delegate), "epoch": 2,
                        "policy_path": "provider-transition/epochs/2/delegate-policy.txt"}
        new = {**copy.deepcopy(old), "delegate_sha256": canon(new_delegate), "epoch": 2,
               "original_preparation": "preparation.json", "original_preparation_sha256": old_sha}
        new["server"]["args"] = ["new-adapter"]
        evidence = {"room_id": self.room, "state_sha256": canon(before),
                    "preparation": {"path": "preparation.json", "sha256": old_sha,
                                    "worktree": str(self.workspace)},
                    "delegate": {"sha256": canon(self.delegate), "record": copy.deepcopy(self.delegate),
                                 "files": {}, "policy_path": "p", "policy_sha256": "1" * 64},
                    "source": {"provider": "deepseek", "adapter_path": "/synthetic/adapter",
                               "adapter_sha256": "2" * 64},
                    "target": {"profile": {"provider": "deepseek"}},
                    "spec": {"version": 1, "sha256": "3" * 64}, "agreement": {"sha256": "4" * 64},
                    "handoff": {"sha256": "5" * 64}, "candidate": {"commit": "6" * 40},
                    "native": {"provider_conversation_id": NATIVE_UUID}, "reviewer_native": None,
                    "engineer": copy.deepcopy(before["bindings"]["engineer"]),
                    "reviewer": copy.deepcopy(before["bindings"]["reviewer"]),
                    "accounting": {"attempts": 1}, "ledger": {}, "probes": {},
                    "shared_registration_rooms": [], "original_launch": {"present": False}}
        saved = {"evidence": evidence, "recorded_at": 1767225600.0}
        audit_sha = canon(saved)
        inputs = {"request_id": "provider-change", "audit_sha256": audit_sha,
                  "authorization": "synthetic-authorization", "diagnosis": "synthetic-diagnosis",
                  "native_stop_record": "synthetic-stop"}
        epoch = {"epoch": 2, "room_id": self.room, **inputs, "recorded_at": 1767225601.0,
                 "before_state_sha256": evidence["state_sha256"],
                 **{key: copy.deepcopy(evidence[key]) for key in ("spec", "agreement", "handoff", "candidate",
                    "native", "reviewer_native", "engineer", "reviewer", "accounting", "original_launch",
                    "shared_registration_rooms")},
                 "source": {**evidence["source"], "preparation": "preparation.json", "preparation_sha256": old_sha,
                            "delegate_sha256": canon(self.delegate), "policy_sha256": "1" * 64},
                 "target": {"provider": "deepseek", "preparation": "provider-transition/epochs/2/preparation.json",
                            "preparation_sha256": canon(new), "delegate_sha256": canon(new_delegate)},
                 "meaning": "Synthetic provider-only amendment; native owners are retained"}
        manifest = {"provider-transition/epochs/2/preparation.json": self.put("provider-transition/epochs/2/preparation.json", new),
                    "provider-transition/epochs/2/epoch.json": self.put("provider-transition/epochs/2/epoch.json", epoch)}
        self.put("provider-transition/audits/" + audit_sha + ".json", saved)
        # These are the actual immutable baseline producer record APIs, never their live validators.
        receipt = transition._receipt(before, inputs, evidence, 1767225601.0, manifest, epoch, new, new_delegate)
        self.put("provider-transition/requests/provider-change.json", receipt)
        ref = {"request_id": inputs["request_id"], "key": canon(inputs),
               "receipt": "provider-transition/requests/provider-change.json", "receipt_sha256": canon(receipt),
               "epoch": 2, "epoch_record": "provider-transition/epochs/2/epoch.json", "epoch_sha256": canon(epoch),
               "original_preparation": "preparation.json", "original_preparation_sha256": old_sha,
               "original_delegate_sha256": canon(self.delegate), "state": "committed"}
        self.state.update(delegate=new_delegate, preparation="provider-transition/epochs/2/preparation.json",
                          preparation_sha256=canon(new), provider_transition=ref)
        self.history_records = {"source": old, "target": new, "epoch": epoch, "transition_receipt": receipt,
                                "transition_audit": saved}
        if adopt:
            before_adoption = copy.deepcopy(self.state)
            saved_adoption = {"evidence": {"room_id": self.room, "state": before_adoption,
                "state_sha256": canon(before_adoption), "provider_epoch_sha256": canon(epoch),
                "provider_epoch": copy.deepcopy(epoch), "prepared": copy.deepcopy(new),
                "native": {"provider_conversation_id": NATIVE_UUID}, "candidate": {"commit": "6" * 40},
                "ledger": {}, "original_config": {}, "runtime_files": {}, "guard_sha256": "7" * 64,
                "wrapper_sha256": "8" * 64}, "attachment_id": "synthetic-attachment",
                "recorded_at": 1767225602.0, "foreground_env": {"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"}}
            adoption_sha = canon(saved_adoption)
            adoption_inputs = {"request_id": "routing-change", "audit_sha256": adoption_sha,
                               "authorization": "synthetic-authorization"}
            adopted = copy.deepcopy(new)
            adopted["routing"].update(version=2, guard_sha256="9" * 64)
            adopted["mcp_attachment"] = {"id": "synthetic-attachment"}
            adopted["server"]["args"] = ["attachment-wrapper", "new-adapter"]
            preparation_data = (json.dumps(adopted, sort_keys=True, indent=2, ensure_ascii=False,
                                           allow_nan=False) + "\n").encode()
            adoption_receipt = adoption._record(self.room, adoption_inputs, before_adoption, adopted, {},
                                                {"routing-adoption/v2/preparation.json": preparation_data})
            self.put("routing-adoption/audits/" + adoption_sha + ".json", saved_adoption)
            self.put("routing-adoption/requests/routing-change.json", adoption_receipt)
            self.put("routing-adoption/v2/preparation.json", adopted)
            self.state.update(preparation="routing-adoption/v2/preparation.json", preparation_sha256=canon(adopted),
                routing_adoption={"request_id": "routing-change", "key": canon(adoption_inputs),
                                  "receipt": "routing-adoption/requests/routing-change.json",
                                  "receipt_sha256": canon(adoption_receipt), "phase": "configured"})
            self.history_records.update(adopted=adopted, adoption_receipt=adoption_receipt,
                                        adoption_audit=saved_adoption)
        self.write_state()

    def select_new_epoch_request(self):
        self.request = copy.deepcopy(self.request)
        self.request_id = "audit-request-epoch2"
        self.request.update(request_id=self.request_id, provider_epoch=2)
        self.state["requests"][self.request_id] = self.request
        self.rewrite_receipt(self.receipt)

    def expected_owner(self, prepared):
        return canon({"native_owner": self.owner_row, "preparation_sha256": canon(prepared)})


class PreparationIdentityTests(HistoryFixture, unittest.TestCase):
    def test_real_initial_profile_projects_path_and_distinct_ao_native_ids(self):
        self.build()
        self.write_transcript([self.human()])
        report = self.audit_in_process()
        self.assertEqual(report["coverage"], "complete")
        self.assertEqual(report["owner_sha256"], self.expected_owner(self.prepared))
        self.assertEqual(report["sources"][0]["source_sha256"], hashlib.sha256(self.transcript.read_bytes()).hexdigest())

    def test_original_request_uses_source_preparation_through_both_real_chains(self):
        for adopted in (False, True):
            with self.subTest(adopted=adopted):
                self.reset()
                self.history(adopt=adopted)
                self.write_transcript([self.human()])
                with mock.patch.object(transition, "validate", side_effect=AssertionError("live validator")), \
                     mock.patch.object(adoption, "validate", side_effect=AssertionError("live validator")):
                    report = self.audit_in_process()
                self.assertEqual(report["coverage"], "complete", report["reasons"])
                self.assertEqual(report["owner_sha256"], self.expected_owner(self.history_records["source"]))
                self.assertNotEqual(report["owner_sha256"], self.expected_owner(self.history_records["target"]))

    def test_new_epoch_request_selects_target_or_configured_adoption(self):
        for adopted in (False, True):
            with self.subTest(adopted=adopted):
                self.reset()
                self.history(adopt=adopted)
                self.select_new_epoch_request()
                self.write_transcript([self.human()])
                report = self.audit_in_process()
                self.assertEqual(report["coverage"], "complete", report["reasons"])
                selected = self.history_records["adopted" if adopted else "target"]
                self.assertEqual(report["owner_sha256"], self.expected_owner(selected))

    def test_every_retained_chain_record_is_required_even_for_historical_request(self):
        for relative in ("provider-transition/requests/provider-change.json", "provider-transition/epochs/2/epoch.json",
                         "preparation.json", "provider-transition/epochs/2/preparation.json",
                         "routing-adoption/requests/routing-change.json", "routing-adoption/v2/preparation.json",
                         "transition-audit", "adoption-audit"):
            with self.subTest(relative=relative):
                self.reset()
                self.history(adopt=True)
                self.write_transcript([self.human()])
                if relative == "transition-audit":
                    relative = "provider-transition/audits/" + canon(self.history_records["transition_audit"]) + ".json"
                elif relative == "adoption-audit":
                    relative = "routing-adoption/audits/" + canon(self.history_records["adoption_audit"]) + ".json"
                (self.home / "ao" / "rooms" / self.room / relative).unlink()
                report = self.audit_in_process()
                self.assertEqual(report["reasons"], ["preparation_unbound"])
                self.assertEqual(report["sources"], [])

    def test_unknown_epoch_pending_adoption_and_retained_owner_drift_refuse(self):
        variants = ("epoch-bool", "epoch-float", "epoch-unknown", "pending", "wrong-key", "old-delegate",
                    "current-preparation", "request-owner")
        for variant in variants:
            with self.subTest(variant=variant):
                self.reset()
                self.history(adopt=True)
                self.write_transcript([self.human()])
                if variant.startswith("epoch-"):
                    self.request["provider_epoch"] = {"epoch-bool": True, "epoch-float": 2.0,
                                                       "epoch-unknown": 3}[variant]
                elif variant == "pending":
                    self.state["routing_adoption"]["phase"] = "pending"
                elif variant == "wrong-key":
                    self.state["provider_transition"]["key"] = "0" * 64
                elif variant == "old-delegate":
                    self.state["provider_transition"]["original_delegate_sha256"] = "0" * 64
                elif variant == "current-preparation":
                    self.state["preparation"] = "provider-transition/epochs/2/preparation.json"
                else:
                    self.request["session_id"] = "changed-owner"
                self.write_state()
                self.assertEqual(self.audit_in_process()["reasons"], ["preparation_unbound"])

    def test_rehashed_adoption_cannot_bind_to_other_epoch_or_float_epoch(self):
        for field in ("provider_epoch", "before_state", "prepared_epoch"):
            with self.subTest(field=field):
                self.reset()
                self.history(adopt=True)
                self.write_transcript([self.human()])
                saved = self.history_records["adoption_audit"]
                receipt = self.history_records["adoption_receipt"]
                if field == "provider_epoch":
                    saved["evidence"]["provider_epoch"]["target"]["delegate_sha256"] = "0" * 64
                elif field == "before_state":
                    saved["evidence"]["state"]["bindings"]["engineer"]["session_id"] = "other-owner"
                    changed = canon(saved["evidence"]["state"])
                    saved["evidence"]["state_sha256"] = changed
                    receipt["before_state_sha256"] = changed
                else:
                    adopted = self.history_records["adopted"]
                    adopted["epoch"] = 2.0
                    self.put("routing-adoption/v2/preparation.json", adopted)
                    receipt["target_preparation_sha256"] = canon(adopted)
                    self.state["preparation_sha256"] = canon(adopted)
                saved_sha = canon(saved)
                self.put("routing-adoption/audits/" + saved_sha + ".json", saved)
                receipt["inputs"]["audit_sha256"] = saved_sha
                self.put("routing-adoption/requests/routing-change.json", receipt)
                self.state["routing_adoption"].update(key=canon(receipt["inputs"]), receipt_sha256=canon(receipt))
                self.write_state()
                self.assertEqual(self.audit_in_process()["reasons"], ["preparation_unbound"])

    def test_preparation_canonical_digest_survives_reformatting(self):
        self.history(adopt=True)
        self.write_transcript([self.human()])
        path = self.home / "ao" / "rooms" / self.room / "preparation.json"
        path.write_text(json.dumps(self.history_records["source"], separators=(",", ":")), encoding="utf-8")
        report = self.audit_in_process()
        self.assertEqual(report["coverage"], "complete")
        self.assertEqual(report["owner_sha256"], self.expected_owner(self.history_records["source"]))


class BindingRefusalTests(AuditFixture, unittest.TestCase):
    def test_shared_projection_helper_and_positive_native_id_boundary(self):
        sentinel = {"request_id": "synthetic"}
        with mock.patch.object(metrics, "receipt_projection_sha256", return_value="d" * 64) as shared:
            self.assertEqual(audit._projection_binding(sentinel), "d" * 64)
            shared.assert_called_once_with(sentinel)
        self.build(request_overrides={"provider_turn_id": "   "})
        self.assertEqual(self.audit_in_process()["reasons"], ["request_integrity"])

    def test_strict_json_rejects_escaped_surrogates_before_binding_digest(self):
        for target in ("state", "receipt", "preparation"):
            for value in ({"metadata": "\ud800"}, {"\udfff": "metadata"}):
                with self.subTest(target=target, kind=list(value)[0].encode("unicode_escape")):
                    self.reset()
                    self.build()
                    self.write_transcript([self.human()])
                    room = self.home / "ao" / "rooms" / self.room
                    path = room / {"state": "state.json", "receipt": self.request["receipt"],
                                   "preparation": "preparation.json"}[target]
                    record = json.loads(path.read_text())
                    record.update(value)
                    path.write_text(json.dumps(record, ensure_ascii=True))
                    result = self.audit_in_process()
                    self.assertEqual(result["reasons"], [{"state": "request_integrity", "receipt": "receipt_integrity",
                                                          "preparation": "preparation_unbound"}[target]])

    def test_huge_json_integer_timestamps_and_owner_strings_fail_closed(self):
        self.build(request_overrides={"created_at": 10**400})
        self.assertEqual(self.audit_in_process()["reasons"], ["request_integrity"])
        self.reset()
        self.build(observed_at=10**400)
        self.assertEqual(self.audit_in_process()["reasons"], ["receipt_integrity"])
        self.reset()
        self.build(owner_activity="x" * (fs.MAX_IDENTITY_BYTES + 1))
        self.assertEqual(self.audit_in_process()["reasons"], ["owner_unavailable"])

    def test_arbitrary_native_prose_is_not_restricted_to_identity_string_limit(self):
        self.build()
        self.write_transcript([self.human(), self.parent_record("assistant", "prose", "2026-01-01T00:00:20+00:00",
                                                              "a" * (fs.MAX_IDENTITY_BYTES + 1))])
        self.assertEqual(self.audit_in_process()["coverage"], "complete")

    def test_native_escaped_surrogates_leave_source_incomplete(self):
        for extra in ({"optional": "\ud800"}, {"\udfff": "optional"}):
            with self.subTest(kind=list(extra)[0].encode("unicode_escape")):
                self.reset()
                self.build()
                self.write_transcript([self.human()])
                malformed = self.parent_record("assistant", "invalid", "2026-01-01T00:00:20+00:00", "text")
                malformed.update(extra)
                with self.transcript.open("ab") as writer:
                    writer.write(json.dumps(malformed, ensure_ascii=True).encode() + b"\n")
                result = self.audit_in_process()
                self.assertEqual(result["coverage"], "incomplete")
                self.assertIn("source_malformed", result["reasons"])

    def test_source_path_dot_components_are_rejected_before_any_open(self):
        self.build()
        for field in ("home", "database"):
            with self.subTest(field=field):
                args = {"home": self.home, "room": self.room, "request_id": self.request_id,
                        "database": self.database, "evidence_root": self.evidence}
                args[field] = str(self.base / "link") + "/../" + Path(args[field]).name
                with mock.patch.object(fs, "_open_root", side_effect=AssertionError("path was opened")):
                    with self.assertRaises(audit.AuditError) as caught:
                        audit.audit(**args)
                self.assertEqual(caught.exception.code, field + "_invalid")

    def test_evidence_root_is_only_lexically_normalized_and_never_opened(self):
        self.build()
        self.write_transcript([self.human()])
        result = audit.audit(self.home, self.room, self.request_id, self.database,
                             str(self.base / "does-not-exist") + "/../evidence")
        self.assertEqual(result["coverage"], "complete")
        self.assertFalse((self.base / "does-not-exist").exists())

    def test_home_and_database_symlink_ancestors_refuse_without_sqlite_open(self):
        self.build()
        link = self.base / "linked"
        link.symlink_to(self.base, target_is_directory=True)
        with mock.patch.object(audit.sqlite3, "connect", side_effect=AssertionError("unsafe SQLite open")):
            result = audit.audit(str(link / "home"), self.room, self.request_id, self.database, self.evidence)
            self.assertEqual(result["reasons"], ["request_integrity"])
            result = audit.audit(self.home, self.room, self.request_id, link / "ao.db", self.evidence)
            self.assertEqual(result["reasons"], ["owner_unavailable"])

    def test_binding_replacement_after_source_collection_refuses(self):
        self.build()
        self.write_transcript([self.human()])
        actual = fs.rehash_source
        def replace_binding(*args, **kwargs):
            result = actual(*args, **kwargs)
            path = self.home / "ao" / "rooms" / self.room / self.request["receipt"]
            replacement = path.with_suffix(".replacement")
            replacement.write_bytes(path.read_bytes())
            replacement.replace(path)
            return result
        with mock.patch.object(fs, "rehash_source", side_effect=replace_binding):
            result = self.audit_in_process()
        self.assertEqual(result["reasons"], ["request_integrity"])
        self.assertEqual(result["sources"], [])


class DescriptorBoundsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        (self.base / "nested").mkdir()
        self.path = self.base / "nested" / "source.jsonl"
        self.path.write_bytes(b"{}\n")
        self.root = fs.Root(self.base, "missing", "unsafe")
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self.root.close)

    def opened(self):
        return self.root.open_file(("nested", "source.jsonl"), 1000, "missing", "unsafe", "oversize")

    def collector(self, extent=1000):
        limits = {"bytes": 1000, "aggregate": extent, "record": 1000, "records": 100,
                  "record_ids": 100, "tool_ids": 100, "depth": 64}
        return native.Collector(limits, set()), limits

    def test_leaf_uses_unbuffered_fileio_and_growth_reads_only_admitted_extent(self):
        self.path.write_bytes(b"x")
        opened = self.opened()
        self.assertIsInstance(opened.handle, io.FileIO)
        self.path.write_bytes(b"x" * 101)
        collector, limits = self.collector(1)
        reads = []
        raw_read = opened.handle.read
        def read(amount):
            data = raw_read(amount)
            reads.append((amount, len(data), os.lseek(opened.handle.fileno(), 0, os.SEEK_CUR)))
            return data
        with mock.patch.object(opened.handle, "read", side_effect=read):
            with self.assertRaises(fs.SourceError) as caught:
                fs.scan_source(opened, lambda *_: None, collector, limits)
        self.assertEqual(caught.exception.reason, "source_changed")
        self.assertEqual(reads, [(1, 1, 1)])
        self.assertEqual(collector.aggregate, 1)
        self.assertTrue(opened.handle.closed)

    def test_second_pass_has_no_extra_eof_read_even_when_file_grows(self):
        collector, limits = self.collector(3)
        first = fs.scan_source(self.opened(), lambda *_: None, collector, limits)
        opened = self.opened()
        raw_read = opened.handle.read
        reads = []
        def grow_after_read(amount):
            data = raw_read(amount)
            reads.append((amount, len(data)))
            with self.path.open("ab") as writer:
                writer.write(b"extra")
            return data
        collector.new_pass()
        with mock.patch.object(opened.handle, "read", side_effect=grow_after_read):
            with self.assertRaises(fs.SourceError) as caught:
                fs.rehash_source(opened, first, collector, limits)
        self.assertEqual(caught.exception.reason, "source_changed")
        self.assertEqual(reads, [(3, 3)])
        self.assertEqual(collector.aggregate, 3)

    def test_early_refusals_close_leaf_and_all_local_parent_descriptors(self):
        for phase in ("first-aggregate", "second-aggregate", "second-signature"):
            with self.subTest(phase=phase):
                collector, limits = self.collector(2)
                opened = self.opened()
                descriptors = [opened.handle.fileno()] + list(opened.chain.fds)
                expected = fs.ScanResult("0" * 64, 3, fs.signature(opened.info), 1, False)
                if phase == "second-signature":
                    expected.signature = (0,) * 8
                with self.assertRaises(fs.SourceError):
                    if phase == "first-aggregate":
                        fs.scan_source(opened, lambda *_: None, collector, limits)
                    else:
                        fs.rehash_source(opened, expected, collector, limits)
                self.assertEqual(collector.aggregate, 0)
                for descriptor in descriptors:
                    with self.assertRaises(OSError):
                        os.fstat(descriptor)
                self.root.recheck_identity("changed")

    def test_parent_name_replacement_during_read_is_detected(self):
        opened = self.opened()
        raw_read = opened.handle.read
        def switch_parent(amount):
            value = raw_read(amount)
            (self.base / "nested").rename(self.base / "old-nested")
            (self.base / "nested").mkdir()
            (self.base / "nested" / "source.jsonl").write_bytes(b"foreign\n")
            return value
        collector, limits = self.collector()
        with mock.patch.object(opened.handle, "read", side_effect=switch_parent):
            with self.assertRaises(fs.SourceError) as caught:
                fs.scan_source(opened, lambda *_: None, collector, limits)
        self.assertEqual(caught.exception.reason, "source_changed")
        self.assertEqual(collector.aggregate, 3)

    def test_all_absolute_ancestors_are_no_follow(self):
        (self.base / "link").symlink_to(self.base, target_is_directory=True)
        with self.assertRaises(fs.SourceError) as caught:
            fs.Root(self.base / "link" / "nested", "missing", "unsafe")
        self.assertEqual(caught.exception.reason, "unsafe")

    def test_binding_growth_never_reads_beyond_its_admitted_extent(self):
        opened = self.opened()
        raw_read, reads = opened.handle.read, []
        def grow_then_read(amount):
            with self.path.open("ab") as writer:
                writer.write(b"x" * 100)
            value = raw_read(amount)
            reads.append((len(value), os.lseek(opened.handle.fileno(), 0, os.SEEK_CUR)))
            return value
        with mock.patch.object(self.root, "open_file", return_value=opened), \
             mock.patch.object(opened.handle, "read", side_effect=grow_then_read):
            with self.assertRaises(fs.SourceError) as caught:
                fs.read_binding_file(self.root, ("nested", "source.jsonl"), 3, "missing", "unsafe", "oversize", "changed")
        self.assertEqual(caught.exception.reason, "changed")
        self.assertEqual(reads, [(3, 3)])
        self.assertTrue(opened.handle.closed)

    def test_raw_failed_record_bytes_are_charged_and_bound(self):
        self.path.write_bytes(b"12345678901234567890\n")
        collector, limits = self.collector(21)
        limits["record"] = 5
        opened = self.opened()
        raw_read, reads = opened.handle.read, []
        def read(amount):
            result = raw_read(amount)
            reads.append(len(result))
            return result
        with mock.patch.object(opened.handle, "read", side_effect=read):
            with self.assertRaises(fs.SourceError) as caught:
                fs.scan_source(opened, lambda *_: None, collector, limits)
        self.assertEqual(caught.exception.reason, "record_bytes_limit")
        self.assertEqual(collector.aggregate, sum(reads))
        self.assertLessEqual(collector.aggregate, 21)


class FinalStabilityTests(AuditFixture, unittest.TestCase):
    def parent_and_child(self):
        self.build()
        self.write_transcript([self.human(),
            self.assistant_tools("launch", "2026-01-01T00:00:20+00:00", [
                {"type": "tool_use", "id": "agent-tool", "name": "Agent", "input": {"prompt": "inspect"}}]),
            self.user_results("completed", "2026-01-01T00:00:30+00:00", [
                {"type": "tool_result", "tool_use_id": "agent-tool", "content": "Finished."}],
                toolUseResult=completed_result("a1", "inspect"))])
        self.write_child("a1", [self.child_record("assistant", "child-read", "2026-01-01T00:00:25+00:00", [
            {"type": "tool_use", "id": "read-1", "name": "Read", "input": {"file_path": self.target()}}]),
            self.child_record("user", "child-result", "2026-01-01T00:00:26+00:00", [
                {"type": "tool_result", "tool_use_id": "read-1", "content": "ok"}])])

    def test_parent_mutated_after_its_rehash_is_caught_after_last_child(self):
        self.parent_and_child()
        self.assertEqual(self.audit_in_process()["coverage"], "complete")
        actual, passed = fs.rehash_source, []
        def mutate_earlier(opened, *args):
            name = opened.name
            result = actual(opened, *args)
            passed.append(name)
            if name == "agent-a1.jsonl":
                with self.transcript.open("ab") as writer:
                    writer.write(b"\n")
            return result
        with mock.patch.object(fs, "rehash_source", side_effect=mutate_earlier):
            report = self.audit_in_process()
        self.assertEqual(passed, [NATIVE_UUID + ".jsonl", "agent-a1.jsonl"])
        self.assertEqual(report["coverage"], "incomplete")
        self.assertIn("source_changed", report["reasons"])
        self.assertIn("source_changed", report["parent"]["reasons"])

    def test_new_duplicate_parent_in_existing_discovery_directory_is_detected(self):
        self.build()
        self.write_transcript([self.human()])
        other_project = self.config_root / "projects" / "other-existing-project"
        other_project.mkdir()
        actual, content_reads = fs.rehash_source, []
        def add_duplicate(opened, *args):
            content_reads.append(opened.name)
            result = actual(opened, *args)
            (other_project / (NATIVE_UUID + ".jsonl")).write_bytes(self.transcript.read_bytes())
            return result
        with mock.patch.object(fs, "rehash_source", side_effect=add_duplicate):
            report = self.audit_in_process()
        self.assertEqual(content_reads, [NATIVE_UUID + ".jsonl"])
        self.assertEqual(report["coverage"], "incomplete")
        self.assertIn("source_changed", report["reasons"])
        self.assertEqual(len(report["sources"]), 1)


if __name__ == "__main__":
    unittest.main()
