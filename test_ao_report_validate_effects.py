"""Zero-effect proofs for the public ao_report_validate surface, pinning project_room_mcp.py's one
Change-1 fix (candidate 0a2c8d12fb9ba75ccfa31311398800b3edd6507d, packet-3 R3): handle()'s tools/call
branch must consult _legacy_tools_visible(service) -- which may open, and so create, the legacy sqlite
registry -- only after confirming the requested name is an actual legacy (non-ao_*) tool, never
unconditionally first. Before that fix, a configured-but-unprovisioned controller home (ao/config.json
present, registry.sqlite3 never created) got an empty registry.sqlite3 created by every single
ao_report_validate call, including the admission-refusal case, exactly as
probe-mcp-draft-effects-r1.py demonstrated.

This module follows test_ao_report_validation.py's own import/fixture style, reusing its
AgreedRoomFixture (via test_ao_lifecycle_contract), full_report/_FIXTURES_BY_NAME fixtures (by
importing that module rather than retyping its sanitized, sha256-pinned literals) and
test_ao_normal.DelegateFixture for the one section (D4) that needs a DeepSeek-provider room. No live
receipts, project data, IDs or room context; no network, provider or model call.
"""
import hashlib
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

import ao_delegates
import ao_project_room
import ao_report_contract
import ao_report_validation
import ao_workflow
import deepseek_adapter
import project_room
import project_room_mcp
from room import RoomError
from test_ao_lifecycle_contract import AgreedRoomFixture
from test_ao_normal import DelegateFixture
import test_ao_report_validation as rv

REPO_ROOT = Path(__file__).resolve().parent

# The same compact-valid shape probe-mcp-draft-effects-r1.py's own `valid = json.dumps({...})` literal
# encodes, reused from test_ao_report_validation's already sha256-pinned fixture (byte-for-byte
# different whitespace, identical fields) rather than retyping a second copy of the same literal.
VALID_TEXT = rv._FIXTURES_BY_NAME["compact-valid.json"]["text"]
# ao_project_room.nonempty's exact admission message for report_text (ao_report_validation.MAX_TEXT_BYTES
# == 1_000_000); also asserted directly by test_ao_report_validation.AdmissionTests.
ADMISSION_MESSAGE = "report_text must be nonempty text, at most 1000000 UTF-8 bytes"


def _snapshot(root):
    """relative path -> {mode, size, sha256} for every regular file under root; the same shape
    probe-mcp-draft-effects-r1.py's own snapshot() helper returns, so these tests observe the
    worktree exactly as the bug report did."""
    return {
        str(path.relative_to(root)): {
            "mode": stat.S_IMODE(path.stat().st_mode),
            "size": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in root.rglob("*") if path.is_file()
    }


def _compact_draft(**fields):
    """A minimal valid compact (project_room_engineering_v2) draft; override/add fields via fields."""
    draft = {"report_format": ao_report_contract.FORMAT, "outcome": "completed",
             "implementation_complete": True, "changes": [], "tests_reported": [],
             "review_findings": [], "remaining_gaps": [], "backlog": [], "routing_log": []}
    draft.update(fields)
    return draft


def _probe_double(home):
    """The exact object.__new__ Service double probe-mcp-draft-effects-r1.py uses: no __init__ runs,
    so no registry.sqlite3 is created merely by constructing this double."""
    service = object.__new__(project_room.Service)
    service.home = home
    service._progress_cache = {}
    service._legacy_tools_visible_cache = None
    service.process_inspector = None
    return service


class ConfiguredWithoutRegistryMcpEffectsTests(unittest.TestCase):
    """Section A: the operator's exact probe reproduced as a test (configured-but-unprovisioned
    controller home: ao/config.json sets default_backend to "ao", but registry.sqlite3 was never
    created)."""

    def test_configured_without_registry_ao_report_validate_has_zero_filesystem_effect(self):
        """Mirrors probe-mcp-draft-effects-r1.py: a fresh temp home with only ao/config.json =
        {"default_backend": "ao"} (no registry.sqlite3), for each of the compact-valid,
        syntax-shape-refusal ("{}") and admission-refusal ("") tools/call cases. Before Change 1, all
        three created an empty registry.sqlite3 because handle() (project_room_mcp.py) evaluated
        _legacy_tools_visible(service) before even checking the tool name; the fixed handle() must
        leave the configured-but-unprovisioned home exactly as it found it in every case, and the
        admission-refusal case's error text must equal ao_project_room.nonempty's own message."""
        cases = (
            ("syntax_shape_success", VALID_TEXT, False),
            ("syntax_shape_refusal", "{}", False),
            ("admission_refusal", "", True),
        )
        # One durable outer temp directory for the whole test (never closed mid-loop), holding one
        # independent, freshly configured home per case, so the post-loop legacy-tool control below
        # can still reach the last case's home on disk.
        with tempfile.TemporaryDirectory(prefix="validator-mcp-effects-") as tmp:
            root = Path(tmp)
            last_home = last_service = None
            for case, text, is_error in cases:
                with self.subTest(case=case):
                    home = root / case
                    (home / "ao").mkdir(parents=True)
                    (home / "ao" / "config.json").write_text(json.dumps({"default_backend": "ao"}))
                    service = _probe_double(home)
                    before = _snapshot(home)
                    response = project_room_mcp.handle(
                        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": "ao_report_validate", "arguments": {"report_text": text}}},
                        service)
                    after = _snapshot(home)
                    self.assertEqual(after, before, case)
                    self.assertNotIn("registry.sqlite3", after)
                    self.assertEqual(response["result"]["isError"], is_error, case)
                    if is_error:
                        self.assertEqual(json.loads(response["result"]["content"][0]["text"]),
                                          {"error": ADMISSION_MESSAGE})
                    last_home, last_service = home, service

            self._assert_legacy_control(last_home, last_service)

    def _assert_legacy_control(self, last_home, last_service):
        # Historical-tool control, on that same (last) home: a legacy (non-ao_*) tool name must still
        # consult _legacy_tools_visible -- Change 1 only reorders the conjunction so the gate is
        # skipped for an ao_* (or unknown) name, it never stops being consulted for an actual legacy
        # name. On this exact schema-less double, Service.legacy_tools_visible()'s blanket
        # `except Exception: visible = True` (project_room.py) swallows the sqlite3.OperationalError a
        # SELECT against the never-created `rooms` table raises, so visibility resolves True (tools
        # are NOT hidden) rather than returning project_room_mcp.LEGACY_TOOLS_HIDDEN_ERROR, which only
        # a schema-bearing registry produces (see
        # test_mcp_legacy_tool_visibility.LegacyToolVisibilityTests
        # .test_hidden_legacy_call_is_refused_without_calling_service_or_writing for that contrasting
        # case). Consulting the gate still has its documented, pre-existing, Change-1-unrelated side
        # effect of creating registry.sqlite3 (empty) as a byproduct of opening it; this test pins
        # that real, unchanged behavior honestly rather than asserting a hidden-tools message this
        # particular double never actually produces.
        self.assertIsNone(last_service._legacy_tools_visible_cache)
        legacy_response = project_room_mcp.handle(
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "room_status", "arguments": {}}},
            last_service)
        self.assertTrue(last_service._legacy_tools_visible_cache)
        self.assertTrue((last_home / "registry.sqlite3").exists())
        self.assertEqual((last_home / "registry.sqlite3").stat().st_size, 0)
        self.assertTrue(legacy_response["result"]["isError"])
        self.assertEqual(json.loads(legacy_response["result"]["content"][0]["text"]),
                          {"error": "Invalid arguments; missing=['room_id'], unexpected=[]"})


class ProvisionedLifecycleZeroEffectsTests(AgreedRoomFixture):
    """Section B: the same three ao_report_validate inputs, now run through all three public entry
    points against a real, fully provisioned controller home with a rich AO room history."""

    def test_public_ao_report_validate_entries_have_zero_effect_on_a_rich_provisioned_home(self):
        """handoff (AgreedRoomFixture.build_template, re-run idempotently by implement()) plus one
        implementation send, finished with a valid full report and synced -- the richest state this
        fixture supports cheaply. Direct Service.call, project_room_mcp.handle (on the SAME real
        project_room.Service(self.home)) and the project_room.py CLI subprocess (exit 0, 0, 2) are
        each run in sequence with the compact-valid, "{}" and "" inputs; after every single call the
        controller home tree, the candidate repo tree, len(self.fake.posts) and the public
        ao_room_status result must all still equal the pre-call baseline, including registry.sqlite3's
        own bytes (the registry-PRESENT control: Section A is the registry-absent proof, Section C is
        the home-absent proof)."""
        self.implement()
        request = self.state()["requests"]["implementation"]
        self.assertIn("engineering_record", request)

        controller = project_room.Service(self.home)  # the one real, non-double Service used for (i)/(ii)
        self.assertTrue((self.home / "registry.sqlite3").exists())

        def baseline():
            return {
                "home": _snapshot(self.home),
                "repo": _snapshot(self.repo),
                "posts": len(self.fake.posts),
                "status": self.service.ao_room_status(self.room),
            }

        before = baseline()
        self.assertIn("registry.sqlite3", before["home"])  # registry-present control

        cases = (
            ("syntax_shape_success", VALID_TEXT, False, 0),
            ("syntax_shape_refusal", "{}", False, 0),
            ("admission_refusal", "", True, 2),
        )
        for case, text, is_error, cli_exit in cases:
            with self.subTest(case=case, entry="direct"):
                if is_error:
                    with self.assertRaises(RoomError):
                        controller.call("ao_report_validate", {"report_text": text})
                else:
                    result = controller.call("ao_report_validate", {"report_text": text})
                    self.assertEqual(result["syntax_shape_valid"], case == "syntax_shape_success")
                self.assertEqual(baseline(), before)

            with self.subTest(case=case, entry="mcp"):
                response = project_room_mcp.handle(
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": "ao_report_validate", "arguments": {"report_text": text}}},
                    controller)
                self.assertEqual(response["result"]["isError"], is_error)
                self.assertEqual(baseline(), before)

            with self.subTest(case=case, entry="cli"):
                completed = subprocess.run(
                    [sys.executable, str(REPO_ROOT / "project_room.py"), "--home", str(self.home),
                     "call", "ao_report_validate", "--args", json.dumps({"report_text": text})],
                    capture_output=True, text=True, cwd=str(REPO_ROOT))
                self.assertEqual(completed.returncode, cli_exit, completed.stderr)
                self.assertEqual(baseline(), before)

        self.assertEqual(_snapshot(self.home)["registry.sqlite3"], before["home"]["registry.sqlite3"])


class MissingHomeControlTests(unittest.TestCase):
    """Section C: references (does not duplicate) the fuller existing missing-home proofs in
    test_ao_report_validation.ZeroEffectsTests --
    test_mcp_tools_list_and_tools_call_never_touch_an_unprovisioned_home (tools/list plus three
    tools/call variants plus a direct Service.call, all on a double whose home is never created) and
    the three CLI exit-code proofs (test_cli_call_skips_service_construction_and_creates_no_home,
    test_cli_invalid_arguments_report_the_shared_error_convention_and_still_create_no_home,
    test_cli_recursion_depth_refusal_reports_exit_2_and_still_creates_no_home). This class adds exactly
    the two controls this packet asks for, so provisioned (ProvisionedLifecycleZeroEffectsTests above),
    configured-without-registry (ConfiguredWithoutRegistryMcpEffectsTests above) and missing-home
    controller states are all pinned together in this one module."""

    def test_cli_call_with_a_never_created_home_creates_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "never-created-cli-home"  # deliberately never created
            self.assertFalse(home.exists())
            completed = subprocess.run(
                [sys.executable, str(REPO_ROOT / "project_room.py"), "--home", str(home),
                 "call", "ao_report_validate", "--args", json.dumps({"report_text": VALID_TEXT})],
                capture_output=True, text=True, cwd=str(REPO_ROOT))
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(json.loads(completed.stdout)["syntax_shape_valid"])
            self.assertFalse(home.exists())

    def test_mcp_handle_on_a_double_with_no_config_at_all_creates_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "no-config-home"  # deliberately never created; no ao/ directory at all
            self.assertFalse(home.exists())
            service = _probe_double(home)
            response = project_room_mcp.handle(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                 "params": {"name": "ao_report_validate", "arguments": {"report_text": VALID_TEXT}}},
                service)
            self.assertFalse(response["result"]["isError"], response)
            self.assertTrue(response["result"]["structuredContent"]["syntax_shape_valid"])
            self.assertFalse(home.exists())
            self.assertFalse((home / "ao").exists())


class CompactDraftIdentityContradictionTests(AgreedRoomFixture):
    """D1: a compact (report_format=project_room_engineering_v2) draft that explicitly carries one
    identity-trio field contradicting the real bound handoff, varied one field per test. Binds
    ao_report_contract.project's
        if any(key in report and (report[key] != value or type(report[key]) is not type(value))
               for key, value in metadata.items()):
            raise RoomError('Native report contradicts the exact bound metadata')
    run through the public ao_room_sync capture path (ao_workflow.capture_engineering ->
    ao_workflow.engineering_report -> ao_report_contract.project)."""

    def _real_handoff(self):
        return ao_workflow.handoff_record(self.directory(), self.state())

    def _assert_contradiction(self, **identity):
        text = json.dumps(_compact_draft(**identity))
        validated = ao_report_validation.validate_draft(text)
        self.assertTrue(validated["syntax_shape_valid"], validated)  # a present identity field is type-checked only
        self.send("implementation")
        self.fake.finish("engineer", text)
        self.service.ao_room_sync(self.room)
        request = self.state()["requests"]["implementation"]
        self.assertEqual(request.get("engineering_error"), "Native report contradicts the exact bound metadata")

    def test_wrong_spec_revision_contradicts_the_bound_handoff(self):
        real = self._real_handoff()
        self._assert_contradiction(spec_revision=real["spec_revision"] + 1)

    def test_wrong_spec_sha256_contradicts_the_bound_handoff(self):
        real = self._real_handoff()
        wrong = "0" * 64 if real["spec_sha256"] != "0" * 64 else "1" * 64
        self.assertNotEqual(wrong, real["spec_sha256"])
        self._assert_contradiction(spec_sha256=wrong)

    def test_wrong_baseline_commit_contradicts_the_bound_handoff(self):
        real = self._real_handoff()
        wrong = "f" * 40 if real["baseline_commit"] != "f" * 40 else "e" * 40
        self.assertNotEqual(wrong, real["baseline_commit"])
        self._assert_contradiction(baseline_commit=wrong)

    def test_compact_draft_with_the_exact_real_handoff_identity_is_a_positive_control(self):
        """Positive control: the same compact draft, now carrying the exact bound handoff identity,
        passes ao_report_contract.project's equality check, so capture succeeds and the saved
        engineering record's controller_metadata.authored_by is 'project_room_controller' -- never the
        model."""
        real = self._real_handoff()
        text = json.dumps(_compact_draft(spec_revision=real["spec_revision"], spec_sha256=real["spec_sha256"],
                                          baseline_commit=real["baseline_commit"]))
        validated = ao_report_validation.validate_draft(text)
        self.assertTrue(validated["syntax_shape_valid"], validated)
        self.send("implementation")
        self.fake.finish("engineer", text)
        self.service.ao_room_sync(self.room)
        request = self.state()["requests"]["implementation"]
        self.assertIsNone(request.get("engineering_error"))
        self.assertIn("engineering_record", request)
        record = ao_project_room.read(self.directory() / request["engineering_record"])
        self.assertEqual(record["controller_metadata"]["authored_by"], "project_room_controller")


class FullDraftIdentityMismatchCaptureTests(AgreedRoomFixture):
    """D2: a full (no report_format) draft whose baseline_commit, or separately spec_revision, is
    syntactically well-formed but does not match the bound handoff record. Both disjuncts live on
    ao_workflow.engineering_report's one combined check:
        if (request.get("handoff_sha256") != state["handoff_sha256"]
                or report.get("spec_revision") != record["spec_revision"]
                or report.get("spec_sha256") != record["spec_sha256"]
                or report.get("baseline_commit") != record["baseline_commit"]
                or shape_diagnostics(report, identity_required=True)):
            raise RoomError("Engineering report is incomplete or refers to another spec/handoff")
    The existing wrong-spec_sha256 case on this same line is
    test_ao_report_validation.ProductionPathParityTests.test_wrong_spec_sha256_passes_shape_but_fails_capture_identity
    and is not duplicated here. The positive control (full draft with the real identity) is
    test_ao_report_validation.LifecycleDraftParityTests.test_valid_full_draft_with_real_identity_parity_with_capture,
    also not duplicated here."""

    def test_wrong_baseline_commit_passes_shape_but_fails_capture_identity(self):
        real = ao_workflow.handoff_record(self.directory(), self.state())
        wrong = "f" * 40 if real["baseline_commit"] != "f" * 40 else "e" * 40
        draft = self.report(baseline_commit=wrong)
        text = json.dumps(draft)
        validated = ao_report_validation.validate_draft(text)
        self.assertTrue(validated["syntax_shape_valid"], validated)
        self.send("implementation")
        self.fake.finish("engineer", text)
        self.service.ao_room_sync(self.room)
        request = self.state()["requests"]["implementation"]
        self.assertEqual(request.get("engineering_error"), "Engineering report is incomplete or refers to another spec/handoff")

    def test_wrong_spec_revision_passes_shape_but_fails_capture_identity(self):
        real = ao_workflow.handoff_record(self.directory(), self.state())
        draft = self.report(spec_revision=real["spec_revision"] + 1)
        text = json.dumps(draft)
        validated = ao_report_validation.validate_draft(text)
        self.assertTrue(validated["syntax_shape_valid"], validated)
        self.send("implementation")
        self.fake.finish("engineer", text)
        self.service.ao_room_sync(self.room)
        request = self.state()["requests"]["implementation"]
        self.assertEqual(request.get("engineering_error"), "Engineering report is incomplete or refers to another spec/handoff")


class HandoffIdentityMismatchTests(AgreedRoomFixture):
    """D3: request['handoff_sha256'] != state['handoff_sha256']. Empirically, the public lifecycle
    refuses a second ao_room_handoff while the implementation request it already issued is still
    active (ao_project_room.Service.ao_room_handoff -> self.quiet(state) -> self.settled(...), which
    raises before ao_workflow.handoff() or ao_workflow.engineering_report() ever run), so this
    particular handoff_sha256 mismatch cannot be driven to capture through the public lifecycle without
    editing saved state (not done here, per the task's own instruction). This test drives and records
    that real, exact refusal, and cites -- rather than forces -- the first disjunct of
    ao_workflow.engineering_report's combined identity check,
    `request.get("handoff_sha256") != state["handoff_sha256"]`, as the binding a genuine post-send
    re-handoff would hit if the public lifecycle ever allowed one."""

    def test_rehandoff_after_an_active_send_is_refused_before_a_handoff_mismatch_is_reachable(self):
        self.send("implementation")
        with self.assertRaises(RoomError) as caught:
            self.service.ao_room_handoff(self.room, str(self.repo))
        self.assertEqual(str(caught.exception), "An owned request is active or uncertain; sync it without resending")


class DelegateAttributionTests(DelegateFixture):
    """D4: ao_delegates.verify_delegation's ledger-ownership gate for a well-formed (32-hex)
    delegate job id named in routing_log[0].delegate_job_ids, run through the public ao_room_sync
    capture path. The deepseek_adapter jobs table has no session-level column (only id, room_id and
    other per-job columns; see deepseek_adapter.SCHEMA), so ownership is scoped by room_id alone -- a
    "different session of the same room" distinction does not apply to this ledger and is not tested."""

    def _advance_to_handoff(self):
        self.bind()
        self.agree()
        self.service.ao_room_handoff(self.room, str(self.repo))

    def _draft_naming_job(self, job_id):
        return _compact_draft(routing_log=[{"delegate_job_ids": [job_id]}])

    def _capture(self, draft):
        text = json.dumps(draft)
        validated = ao_report_validation.validate_draft(text)
        self.assertTrue(validated["syntax_shape_valid"], validated)
        self.send("implementation")
        self.fake.finish("engineer", text)
        self.service.ao_room_sync(self.room)
        return self.state()["requests"]["implementation"]

    def test_foreign_room_well_formed_job_id_fails_the_ledger_ownership_gate(self):
        """Binds ao_delegates.verify_delegation's
            except deepseek_adapter.AdapterError as exc:
                raise RoomError("Engineering report names an unknown or foreign delegate job " + job_id) from exc
        A well-formed job id owned by a genuinely different, really-opened AO room fails
        deepseek_adapter.Ledger.job's room_id check before model/profile/state are ever inspected, so
        the foreign row's other columns need not match this room's pinned provider configuration."""
        self._advance_to_handoff()
        # ao_room_open derives room_id deterministically from (project_path, feature)
        # (ao_project_room.py: room_id = "ao-" + digest({"path":..., "feature": feature})[:24]), so a
        # genuinely different room on the SAME self.repo needs a different feature, not just a second
        # open() call with the same default feature (which would idempotently return self.room itself).
        foreign_room = self.open(feature='foreign-room', provider='deepseek')
        self.assertNotEqual(foreign_room, self.room)
        job_id = 'f' * 32
        ledger = deepseek_adapter.Ledger(self.home)
        with ledger.transaction() as db:
            db.execute(
                'INSERT INTO jobs(id,room_id,request_id,lane,payload_sha256,profile_sha256,state,created_at,'
                'requested_model,thinking,reasoning_effort,max_tokens,input_bytes,reserved_bytes,possibly_billed) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (job_id, foreign_room, 'foreign-request', 'deep', 'p' * 64, 'q' * 64, deepseek_adapter.COMPLETED,
                 '2026-09-09T00:00:00+00:00', deepseek_adapter.DEFAULT_MODEL, 'enabled', 'max', 393216, 100, 0, 1))
        request = self._capture(self._draft_naming_job(job_id))
        self.assertEqual(request.get("engineering_error"),
                          "Engineering report names an unknown or foreign delegate job " + job_id)

    def test_same_room_well_formed_job_id_is_a_positive_control(self):
        """Positive control: a well-formed job id owned by THIS room, with requested_model/profile_sha256
        matching this room's pinned provider configuration (ao_delegates.validate_provider,
        ao_delegates.expected_profile) and a non-COMPLETED terminal state (REJECTED, classified
        "non_result" by verify_delegation, so no exported-content digest is needed) passes verification:
        capture succeeds, request['delegate_job_ids'] names exactly this job (the exact field
        ao_workflow.capture_engineering sets from verify_delegation's returned evidence), and
        engineering_record is present."""
        self._advance_to_handoff()
        job_id = '1' * 32
        settings = ao_delegates.validate_provider(self.directory(), self.state())['delegate_settings']
        profile = ao_delegates.expected_profile(self.state()['delegate']['inventory'])
        ledger = deepseek_adapter.Ledger(self.home)
        with ledger.transaction() as db:
            db.execute(
                'INSERT INTO jobs(id,room_id,request_id,lane,payload_sha256,profile_sha256,state,created_at,'
                'requested_model,thinking,reasoning_effort,max_tokens,input_bytes,reserved_bytes,possibly_billed) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (job_id, self.room, 'same-room-request', 'deep', 'p' * 64, profile, deepseek_adapter.REJECTED,
                 '2026-09-09T00:00:00+00:00', settings['model'], 'enabled', 'max', 393216, 100, 0, 1))
        request = self._capture(self._draft_naming_job(job_id))
        self.assertIsNone(request.get("engineering_error"))
        self.assertIn("engineering_record", request)
        self.assertEqual(request.get("delegate_job_ids"), [job_id])


if __name__ == "__main__":
    unittest.main()
