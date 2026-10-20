"""Tests for ao_report_validation: the offline, read-only ao_report_validate draft operation and its
shared-shape parity with ao_workflow.engineering_report (authoritative capture). Synthetic/sanitized
fixtures only (no live receipts, project data, IDs or room context); no network, provider or model call.
"""
import hashlib
import http.client
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import ao_project_room
import ao_report_contract
import ao_report_validation
import ao_workflow
import project_room
import project_room_mcp
from room import RoomError
from test_ao_lifecycle_contract import AgreedRoomFixture, LifecycleFixture
from test_ao_normal import Fixture

REPO_ROOT = Path(__file__).resolve().parent

# Sanitized synthetic draft-report fixtures, authored from the public compact contract and the observed
# defect classes above; not live receipts, project data, IDs or room context, and not test results in
# themselves. Each "text" is byte-for-byte what a native session could return; its recorded "sha256" is
# checked against the live text before the fixture is ever fed to validate_draft, so a future accidental
# edit of one without the other fails loudly instead of silently changing what is being asserted.
FIXTURES = (
    {
        "name": "README.txt",
        "sha256": "059d486dc4ecb91e8a41ab4a84538a5a229dafdf708c184a6d8ce8373f22e075",
        "text": ("Sanitized input shapes only, authored from public contract and observed defect classes. "
                  "No live receipts, project data, IDs or room context. These are not test results. Draft "
                  "validity never demonstrates final-capture eligibility. Enhancement-proposal defects "
                  "remain advisory to authoritative capture; test their visible warning without creating "
                  "a new acceptance blocker. The engineering worker may adapt these inputs into repository "
                  "fixtures after reviewing actual validators.\n"),
        "syntax_shape_valid": False,
        "report_form": None,
        "diagnostics": [{"field": "$", "message": "must be one unambiguous JSON object, optionally inside "
                                                    "a ```json fence; prose is never extracted"}],
        "advisories": [],
    },
    {
        "name": "compact-valid.json",
        "sha256": "a5b935bd71957889872455cbc7a40bd43572cd644126d7cb5a21094a772903ad",
        "text": ('{\n  "report_format": "project_room_engineering_v2",\n  "outcome": "completed",\n'
                  '  "implementation_complete": true,\n  "changes": [],\n  "tests_reported": [],\n'
                  '  "review_findings": [],\n  "remaining_gaps": [],\n  "backlog": [],\n'
                  '  "routing_log": []\n}\n'),
        "syntax_shape_valid": True,
        "report_form": "project_room_engineering_v2",
        "diagnostics": [],
        "advisories": [],
    },
    {
        "name": "controller-metadata-injected.json",
        "sha256": "c159d5d896e7562bf8fde67822f6677f3e0f29c6f43cbfe1bcac8090ff64e64a",
        "text": ('{\n  "report_format": "project_room_engineering_v2",\n  "outcome": "completed",\n'
                  '  "implementation_complete": true,\n  "changes": [],\n  "tests_reported": [],\n'
                  '  "review_findings": [],\n  "remaining_gaps": [],\n  "backlog": [],\n'
                  '  "routing_log": [],\n  "controller_metadata": {\n    "authored_by": "synthetic-model"\n'
                  '  }\n}\n'),
        "syntax_shape_valid": False,
        "report_form": "project_room_engineering_v2",
        "diagnostics": [{"field": "controller_metadata",
                          "message": "The model cannot supply controller-owned report provenance"}],
        "advisories": [],
    },
    {
        "name": "enhancement-object-advisory.json",
        "sha256": "b5b1c6ed7f9a5d11e683509643acc3626d7ff79ef417eb2fbd854d82919e5d47",
        "text": ('{\n  "report_format": "project_room_engineering_v2",\n  "outcome": "completed",\n'
                  '  "implementation_complete": true,\n  "changes": [],\n  "tests_reported": [],\n'
                  '  "review_findings": [],\n  "remaining_gaps": [],\n  "backlog": [],\n'
                  '  "routing_log": [],\n  "enhancement_proposals": {\n    "title": "synthetic proposal"\n'
                  '  }\n}\n'),
        "syntax_shape_valid": True,
        "report_form": "project_room_engineering_v2",
        "diagnostics": [],
        "advisories": [{"field": "enhancement_proposals",
                         "message": "enhancement_proposals must be a list; omit an unreported field or "
                                    "use [] for none"}],
    },
    {
        "name": "operator-requests-string-list.json",
        "sha256": "b2fb21192578d09b4e6f6e86ee501941eafd1096f5491522a4ef197c66080a42",
        "text": ('{\n  "report_format": "project_room_engineering_v2",\n  "outcome": "completed",\n'
                  '  "implementation_complete": true,\n  "changes": [],\n  "tests_reported": [],\n'
                  '  "review_findings": [],\n  "remaining_gaps": [],\n  "backlog": [],\n'
                  '  "routing_log": [],\n  "operator_requests": [\n    "run the affected checks"\n  ]\n}\n'),
        "syntax_shape_valid": False,
        "report_form": "project_room_engineering_v2",
        "diagnostics": [{"field": "operator_requests",
                          "message": "Malformed operator_requests entry; preserve the response and "
                                     "correct the report"}],
        "advisories": [],
    },
    {
        "name": "outcome-invalid.json",
        "sha256": "5e517f5566492cfcf61e0cbfe4cf9e7c484d8d1aa7932dd4bb65b9d51b717793",
        "text": ('{\n  "report_format": "project_room_engineering_v2",\n  "outcome": "partial",\n'
                  '  "implementation_complete": true,\n  "changes": [],\n  "tests_reported": [],\n'
                  '  "review_findings": [],\n  "remaining_gaps": [],\n  "backlog": [],\n'
                  '  "routing_log": []\n}\n'),
        "syntax_shape_valid": False,
        "report_form": "project_room_engineering_v2",
        "diagnostics": [{"field": "outcome",
                          "message": "must be completed, changes_required or scope_change"}],
        "advisories": [],
    },
    {
        "name": "routing-job-list-missing.json",
        "sha256": "ceb27cf358954a258611d2805bd37aa1bfba0b80bc4a634979e4c8b75dd9b487",
        "text": ('{\n  "report_format": "project_room_engineering_v2",\n  "outcome": "completed",\n'
                  '  "implementation_complete": true,\n  "changes": [],\n  "tests_reported": [],\n'
                  '  "review_findings": [],\n  "remaining_gaps": [],\n  "backlog": [],\n'
                  '  "routing_log": [\n    {\n      "worker": "synthetic-worker"\n    }\n  ]\n}\n'),
        "syntax_shape_valid": False,
        "report_form": "project_room_engineering_v2",
        "diagnostics": [{"field": "routing_log[0].delegate_job_ids", "message": "must be a list of strings"}],
        "advisories": [],
    },
    {
        "name": "surrounding-prose.txt",
        "sha256": "17eca26e4d99d437e68747b98441222f618744643c0d5bac04b30f61f60dc123",
        "text": ('Synthetic report follows.\n{"report_format": "project_room_engineering_v2", "outcome": '
                  '"completed", "implementation_complete": true, "changes": [], "tests_reported": [], '
                  '"review_findings": [], "remaining_gaps": [], "backlog": [], "routing_log": []}\n'),
        "syntax_shape_valid": False,
        "report_form": None,
        "diagnostics": [{"field": "$", "message": "must be one unambiguous JSON object, optionally inside "
                                                    "a ```json fence; prose is never extracted"}],
        "advisories": [],
    },
    {
        "name": "tests-object.json",
        "sha256": "f72d4b90c6ad991390af98b386017469210da5b566fe5079339038eeec9ca910",
        "text": ('{\n  "report_format": "project_room_engineering_v2",\n  "outcome": "completed",\n'
                  '  "implementation_complete": true,\n  "changes": [],\n  "tests_reported": {\n'
                  '    "passed": 1\n  },\n  "review_findings": [],\n  "remaining_gaps": [],\n'
                  '  "backlog": [],\n  "routing_log": []\n}\n'),
        "syntax_shape_valid": False,
        "report_form": "project_room_engineering_v2",
        "diagnostics": [{"field": "tests_reported", "message": "must be a list"}],
        "advisories": [],
    },
)

# Convenient lookup by name for lifecycle-driven parity tests that reuse a fixture's exact "text"
# (and its already-digest-verified diagnostics/advisories/syntax_shape_valid) verbatim, instead of
# retyping a second copy of the same sanitized bytes.
_FIXTURES_BY_NAME = {fixture["name"]: fixture for fixture in FIXTURES}


def full_report(**changes):
    """A fully valid full-form (no report_format) engineering report; override fields via changes."""
    report = {"outcome": "completed", "implementation_complete": True, "changes": [], "tests_reported": [],
              "review_findings": [], "remaining_gaps": [], "backlog": [], "routing_log": [],
              "spec_revision": 1, "spec_sha256": "a" * 64, "baseline_commit": "b" * 40}
    report.update(changes)
    return report


class SharedSchemaIdentityTests(unittest.TestCase):
    """The offline validator's field list must name exactly the same fields authoritative capture does."""

    def test_field_order_matches_engineering_fields(self):
        self.assertEqual(set(ao_report_validation.FIELD_ORDER), ao_workflow.ENGINEERING_FIELDS)
        self.assertEqual(len(ao_report_validation.FIELD_ORDER), 11)

    def test_unperformed_checks_are_never_mutated_by_validate_draft(self):
        before = [dict(item) for item in ao_report_validation.UNPERFORMED_CHECKS]
        ao_report_validation.validate_draft(json.dumps(full_report()))
        ao_report_validation.validate_draft("not json")
        self.assertEqual([dict(item) for item in ao_report_validation.UNPERFORMED_CHECKS], before)

    def test_unperformed_checks_names_and_count_are_pinned(self):
        # Independent literal, not merely the module comparing its own tuple to itself: catches an
        # accidental rename, reorder, duplicate or omission of a named check. The first eight are the
        # original authentication/delivery/readiness/persistence checks; the next five accurately
        # name what ao_workflow.capture_engineering additionally performs (completion-candidate
        # binding, native outcome usability, the report-correction chain walk, require_disposition,
        # and the candidate-unchanged-after-completion equality check) that this offline operation
        # never performs either. The final three name deferred authoritative checks that sit beyond
        # capture itself -- semantic correctness assessment, the pinned formal gates (ao_room_verify)
        # and Astra's independent acceptance (ao_room_accept) -- and are always present, even for a
        # completed/true draft with no remaining gaps, so a clean draft is never mistaken for an
        # accepted or capturable one. This list is a reminder, never an exhaustive contract: capture,
        # readiness and acceptance may perform further checks this tuple does not name.
        names = [item["check"] for item in ao_report_validation.UNPERFORMED_CHECKS]
        self.assertEqual(names, [
            "handoff_authentication", "spec_identity_equality", "compact_contract_delivery",
            "controller_metadata_attachment", "delegate_attribution_verification", "engineering_readiness",
            "findings_projection", "capture_receipts", "candidate_binding", "native_outcome_evidence",
            "correction_chain", "issue_dispositions", "candidate_unchanged",
            "semantic_correctness_assessment", "formal_candidate_verification", "independent_acceptance",
        ])
        self.assertEqual(len(names), 16)
        self.assertEqual(len(set(names)), 16)  # every name distinct

        # A completed/true compact draft with empty remaining_gaps (the compact-valid fixture) still
        # returns all 16 deferred checks, including the three new ones: a clean, gap-free draft never
        # implies semantic correctness, formal candidate verification or independent acceptance.
        fixture = _FIXTURES_BY_NAME["compact-valid.json"]
        draft = ao_report_validation.validate_draft(fixture["text"])
        self.assertTrue(draft["syntax_shape_valid"], draft)
        self.assertEqual([item["check"] for item in draft["unperformed_checks"]], names)


class FixtureParityTests(unittest.TestCase):
    """Each sanitized fixture's recorded digest is verified before use, then fed through validate_draft."""

    def test_fixture_digests_and_outcomes(self):
        for fixture in FIXTURES:
            with self.subTest(fixture=fixture["name"]):
                computed = hashlib.sha256(fixture["text"].encode("utf-8")).hexdigest()
                self.assertEqual(computed, fixture["sha256"],
                                  f"{fixture['name']} text no longer matches its recorded sha256")
                result = ao_report_validation.validate_draft(fixture["text"])
                self.assertEqual(result["operation"], "ao_report_validate")
                self.assertEqual(result["validity_scope"], "syntax_and_shape_only")
                self.assertEqual(result["syntax_shape_valid"], fixture["syntax_shape_valid"])
                self.assertEqual(result["report_form"], fixture["report_form"])
                self.assertEqual(result["diagnostics"], fixture["diagnostics"])
                self.assertEqual(result["advisories"], fixture["advisories"])
                self.assertEqual(result["unperformed_checks"],
                                  [dict(item) for item in ao_report_validation.UNPERFORMED_CHECKS])
                self.assertEqual(result["limits"],
                                  {"max_text_bytes": ao_report_validation.MAX_TEXT_BYTES,
                                   "max_depth": ao_report_validation.MAX_DEPTH,
                                   "max_diagnostics": ao_report_validation.MAX_DIAGNOSTICS,
                                   "truncated": False})


class FullFormTests(unittest.TestCase):
    """Full-form (no report_format) reports skip the compact-only checks but keep full shape checking."""

    def test_valid_full_report_with_unknown_field_and_controller_metadata_is_clean(self):
        # A full-form report is never run through refuse_model_metadata (ao_report_contract.project only
        # calls it for compact reports); an unrecognized extra key is likewise never rejected here.
        report = full_report(controller_metadata={"authored_by": "synthetic-model"}, unknown_extra="ignored")
        result = ao_report_validation.validate_draft(json.dumps(report))
        self.assertTrue(result["syntax_shape_valid"])
        self.assertEqual(result["report_form"], "full")
        self.assertEqual(result["diagnostics"], [])

    def test_full_report_missing_identity_trio_is_flagged(self):
        report = full_report()
        for field in ("spec_revision", "spec_sha256", "baseline_commit"):
            del report[field]
        result = ao_report_validation.validate_draft(json.dumps(report))
        self.assertFalse(result["syntax_shape_valid"])
        self.assertEqual(result["report_form"], "full")
        self.assertEqual(result["diagnostics"],
                          [{"field": "spec_revision", "message": "missing required field"},
                           {"field": "spec_sha256", "message": "missing required field"},
                           {"field": "baseline_commit", "message": "missing required field"}])

    def test_unsupported_report_format_is_flagged_and_skips_compact_only_checks(self):
        report = full_report(report_format="some_other_format")
        result = ao_report_validation.validate_draft(json.dumps(report))
        self.assertFalse(result["syntax_shape_valid"])
        self.assertEqual(result["report_form"], "unsupported")
        self.assertEqual(result["diagnostics"],
                          [{"field": "report_format", "message": "Unsupported native engineering report format"}])

    def test_bool_spec_revision_is_a_type_defect_not_just_a_value_mismatch(self):
        report = full_report(spec_revision=True)  # True == 1 but must still be rejected by *type*
        result = ao_report_validation.validate_draft(json.dumps(report))
        self.assertFalse(result["syntax_shape_valid"])
        self.assertEqual(result["diagnostics"], [{"field": "spec_revision", "message": "must be an integer"}])


class AdmissionTests(unittest.TestCase):
    """Only malformed admission raises; a malformed report itself always becomes diagnostics."""

    def test_non_string_or_missing_text_raises(self):
        for value in (None, 12345, [], {}, True):
            with self.subTest(value=value):
                with self.assertRaisesRegex(RoomError, "report_text must be nonempty text"):
                    ao_report_validation.validate_draft(value)

    def test_empty_or_whitespace_only_raises(self):
        for value in ("", "   \n\t  "):
            with self.subTest(value=repr(value)):
                with self.assertRaisesRegex(RoomError, "report_text must be nonempty text"):
                    ao_report_validation.validate_draft(value)

    def test_oversize_text_raises(self):
        oversized = '{"a": "' + ("x" * ao_report_validation.MAX_TEXT_BYTES) + '"}'
        with self.assertRaisesRegex(RoomError, "report_text must be nonempty text, at most "
                                                f"{ao_report_validation.MAX_TEXT_BYTES} UTF-8 bytes"):
            ao_report_validation.validate_draft(oversized)

    def test_oversize_in_bytes_but_undersize_in_characters_raises(self):
        # The bound is on encoded UTF-8 bytes, not characters (ao_project_room.nonempty bounds
        # len(value.encode())): a 2-byte character repeated enough times crosses the byte bound
        # while its character count stays well under the same number.
        oversized = '{"a": "' + ("£" * 600_000) + '"}'  # "£", U+00A3, 2 bytes each in UTF-8
        self.assertLess(len(oversized), ao_report_validation.MAX_TEXT_BYTES)
        self.assertGreater(len(oversized.encode("utf-8")), ao_report_validation.MAX_TEXT_BYTES)
        with self.assertRaisesRegex(RoomError, "report_text must be nonempty text, at most "
                                                f"{ao_report_validation.MAX_TEXT_BYTES} UTF-8 bytes"):
            ao_report_validation.validate_draft(oversized)

    def test_lone_surrogate_raises_utf8_message(self):
        with self.assertRaisesRegex(RoomError, "report_text must be valid UTF-8 text"):
            ao_report_validation.validate_draft("\udc80")

    @staticmethod
    def _nested(depth):
        """depth nested dict containers; the root container alone is depth 1 (ao_quality_review convention)."""
        value = 1
        for _ in range(depth):
            value = {"a": value}
        return json.dumps(value)

    def test_depth_at_the_limit_is_admitted(self):
        result = ao_report_validation.validate_draft(self._nested(ao_report_validation.MAX_DEPTH))
        self.assertFalse(result["syntax_shape_valid"])  # not a report shape, but admission did not raise

    def test_depth_over_the_limit_raises(self):
        with self.assertRaisesRegex(RoomError, "Draft report exceeds the admitted nesting depth"):
            ao_report_validation.validate_draft(self._nested(ao_report_validation.MAX_DEPTH + 1))

    # R1: depths far beyond the interpreter's own recursion limit (about 995 levels on Python 3.11)
    # make the decoder itself raise RecursionError, before strict_object can even return a value for
    # the post-parse _exceeds_max_depth walk to inspect. Built by plain string repetition, never by
    # json.dumps of an actually-nested Python container, because building *that* container (or
    # serializing it back with json.dumps) would itself recurse past the same interpreter limit.

    def test_depth_far_beyond_the_recursion_limit_raises_the_same_depth_refusal(self):
        text = '{"a":' * 2000 + '1' + '}' * 2000
        with self.assertRaisesRegex(RoomError, "Draft report exceeds the admitted nesting depth"):
            ao_report_validation.validate_draft(text)

    def test_deeply_nested_list_inside_an_object_beyond_the_recursion_limit_raises_the_same_refusal(self):
        text = '{"a":' + '[' * 2000 + '1' + ']' * 2000 + '}'
        with self.assertRaisesRegex(RoomError, "Draft report exceeds the admitted nesting depth"):
            ao_report_validation.validate_draft(text)


class MalformedJsonDiagnosticsTests(unittest.TestCase):
    """strict_object rejects ambiguous/invalid JSON; validate_draft turns that into one diagnostic, never a raise."""

    PARSE_DIAGNOSTIC = [{"field": "$", "message": "must be one unambiguous JSON object, optionally inside "
                                                    "a ```json fence; prose is never extracted"}]

    def test_duplicate_keys_are_rejected(self):
        result = ao_report_validation.validate_draft('{"outcome": "completed", "outcome": "changes_required"}')
        self.assertIsNone(result["report_form"])
        self.assertEqual(result["diagnostics"], self.PARSE_DIAGNOSTIC)

    def test_non_finite_numbers_are_rejected(self):
        result = ao_report_validation.validate_draft('{"outcome": NaN}')
        self.assertEqual(result["diagnostics"], self.PARSE_DIAGNOSTIC)

    def test_non_object_root_is_rejected(self):
        result = ao_report_validation.validate_draft("[1, 2, 3]")
        self.assertEqual(result["diagnostics"], self.PARSE_DIAGNOSTIC)

    def test_single_json_fence_is_accepted(self):
        fenced = "```json\n" + json.dumps(full_report()) + "\n```"
        result = ao_report_validation.validate_draft(fenced)
        self.assertTrue(result["syntax_shape_valid"])
        self.assertEqual(result["diagnostics"], [])


class BoundsTests(unittest.TestCase):
    """MAX_DIAGNOSTICS/MAX_MESSAGE_CHARS bound the returned object regardless of how defective the draft is."""

    def test_many_malformed_routing_log_entries_truncate_to_max_diagnostics(self):
        report = full_report(routing_log=[{"worker": "w"} for _ in range(200)])
        result = ao_report_validation.validate_draft(json.dumps(report))
        self.assertEqual(len(result["diagnostics"]), ao_report_validation.MAX_DIAGNOSTICS)
        self.assertTrue(result["limits"]["truncated"])
        self.assertEqual(result["diagnostics"][0],
                          {"field": "routing_log[0].delegate_job_ids", "message": "must be a list of strings"})
        self.assertEqual(result["diagnostics"][-1],
                          {"field": f"routing_log[{ao_report_validation.MAX_DIAGNOSTICS - 1}].delegate_job_ids",
                           "message": "must be a list of strings"})

    def test_bounded_helper_truncates_count_at_the_exact_boundary(self):
        at_limit = [{"field": str(i), "message": "x"} for i in range(ao_report_validation.MAX_DIAGNOSTICS)]
        over_limit = at_limit + [{"field": "extra", "message": "x"}]
        _, truncated_at = ao_report_validation._bounded(at_limit)
        _, truncated_over = ao_report_validation._bounded(over_limit)
        self.assertFalse(truncated_at)
        self.assertTrue(truncated_over)

    def test_bounded_helper_truncates_message_length(self):
        long_message = "x" * (ao_report_validation.MAX_MESSAGE_CHARS + 50)
        bounded, _ = ao_report_validation._bounded([{"field": "f", "message": long_message}])
        self.assertEqual(len(bounded[0]["message"]), ao_report_validation.MAX_MESSAGE_CHARS)


class ProductionPathParityTests(AgreedRoomFixture):
    """Demonstrates the documented boundary: a draft that validate_draft reports as shape-clean can still
    be refused by authoritative capture for a check this offline operation never performs (identity
    equality, delegate attribution). The checks it would be refused for are named verbatim in
    UNPERFORMED_CHECKS, so this is a disclosed gap, not an accidental one."""

    def test_wrong_spec_sha256_passes_shape_but_fails_capture_identity(self):
        bad = self.report(spec_sha256="0" * 64)
        text = json.dumps(bad)
        posts_before = len(self.fake.posts)
        draft = ao_report_validation.validate_draft(text)
        self.assertEqual(len(self.fake.posts), posts_before)  # the validator call itself never posts
        self.assertTrue(draft["syntax_shape_valid"], draft)  # shape alone is fine; identity is unperformed here
        self.send("implementation")
        self.fake.finish("engineer", text)
        self.service.ao_room_sync(self.room)
        request = self.state()["requests"]["implementation"]
        self.assertEqual(request.get("engineering_error"),
                          "Engineering report is incomplete or refers to another spec/handoff")

    def test_malformed_delegate_job_id_passes_shape_but_fails_delegate_attribution(self):
        bad = self.report(routing_log=[{"delegate_job_ids": ["synthetic-malformed-delegate-id"]}])
        text = json.dumps(bad)
        posts_before = len(self.fake.posts)
        draft = ao_report_validation.validate_draft(text)
        self.assertEqual(len(self.fake.posts), posts_before)  # the validator call itself never posts
        self.assertTrue(draft["syntax_shape_valid"], draft)  # a list of strings is shape-valid
        self.send("implementation")
        self.fake.finish("engineer", text)
        self.service.ao_room_sync(self.room)
        request = self.state()["requests"]["implementation"]
        self.assertEqual(request.get("engineering_error"),
                          "Engineering report names a malformed delegate job identifier")


class LifecycleDraftParityTests(AgreedRoomFixture):
    """Further validator/capture parity cases driven through the same public lifecycle as
    ProductionPathParityTests above (handoff, ao_room_send, fake.finish with the draft text as the
    engineer's own final response, ao_room_sync, then inspect the request's engineering_error or
    captured engineering record), covering the remaining documented capture-side checks: shape
    identity after compact projection, operator_requests and controller_metadata blocking, and the
    advisory-never-blocks-capture guarantee for enhancement_proposals. Every validate_draft call here
    is also checked to never post to the fake AO transport."""

    def _validated_without_posting(self, text):
        posts_before = len(self.fake.posts)
        draft = ao_report_validation.validate_draft(text)
        self.assertEqual(len(self.fake.posts), posts_before)  # the validator call itself never posts
        return draft

    def _captured(self, text):
        self.send("implementation")
        self.fake.finish("engineer", text)
        self.service.ao_room_sync(self.room)
        return self.state()["requests"]["implementation"]

    def test_tests_object_fixture_diagnostic_parity_with_capture(self):
        # (a) sanitized tests-object fixture: tests_reported is an object, not a list.
        fixture = _FIXTURES_BY_NAME["tests-object.json"]
        draft = self._validated_without_posting(fixture["text"])
        self.assertEqual(draft["syntax_shape_valid"], fixture["syntax_shape_valid"])
        self.assertEqual(draft["diagnostics"], fixture["diagnostics"])
        request = self._captured(fixture["text"])
        self.assertEqual(request.get("engineering_error"),
                          "Engineering report is incomplete or refers to another spec/handoff")

    def test_full_form_invalid_outcome_with_correct_identity_diagnostic_parity_with_capture(self):
        # (b) a full-form shape-invalid draft (outcome "partial") carrying the handoff's correct
        # identity, so the identity check itself never masks the shape defect in either path.
        text = json.dumps(self.report(outcome="partial"))
        draft = self._validated_without_posting(text)
        self.assertFalse(draft["syntax_shape_valid"], draft)
        self.assertEqual(draft["report_form"], "full")
        self.assertEqual(draft["diagnostics"],
                          [{"field": "outcome", "message": "must be completed, changes_required or scope_change"}])
        request = self._captured(text)
        self.assertEqual(request.get("engineering_error"),
                          "Engineering report is incomplete or refers to another spec/handoff")

    def test_valid_full_draft_with_real_identity_parity_with_capture(self):
        # (c) a valid full draft using the handoff's real spec_revision/spec_sha256/baseline_commit.
        text = json.dumps(self.report())
        draft = self._validated_without_posting(text)
        self.assertTrue(draft["syntax_shape_valid"], draft)
        self.assertEqual(draft["report_form"], "full")
        request = self._captured(text)
        self.assertNotIn("engineering_error", request)
        self.assertIn("engineering_record", request)

    def test_compact_valid_fixture_parity_with_capture(self):
        # (d) the compact-valid fixture verbatim: efficiency_contract_v1 was already delivered on the
        # room's first (spec_review) engineer send, inside AgreedRoomFixture.build_template's agree().
        fixture = _FIXTURES_BY_NAME["compact-valid.json"]
        draft = self._validated_without_posting(fixture["text"])
        self.assertEqual(draft["syntax_shape_valid"], fixture["syntax_shape_valid"])
        self.assertTrue(draft["syntax_shape_valid"], draft)
        request = self._captured(fixture["text"])
        self.assertNotIn("engineering_error", request)
        self.assertIn("engineering_record", request)

    def test_enhancement_object_advisory_fixture_parity_with_capture(self):
        # (e) the enhancement-object-advisory fixture verbatim: the validator reports exactly one
        # advisory and stays valid; capture still produces a record because an advisory never blocks
        # capture -- the required optional-proposal advisory proof.
        fixture = _FIXTURES_BY_NAME["enhancement-object-advisory.json"]
        draft = self._validated_without_posting(fixture["text"])
        self.assertEqual(draft["syntax_shape_valid"], fixture["syntax_shape_valid"])
        self.assertEqual(draft["advisories"], fixture["advisories"])
        self.assertEqual(len(draft["advisories"]), 1)
        request = self._captured(fixture["text"])
        self.assertNotIn("engineering_error", request)
        self.assertIn("engineering_record", request)

    def test_operator_requests_string_list_fixture_blocking_parity_with_capture(self):
        # (f) the operator-requests-string-list fixture: the validator's diagnostic and capture's
        # engineering_error are both exactly the ao_review_followups.validate message.
        fixture = _FIXTURES_BY_NAME["operator-requests-string-list.json"]
        draft = self._validated_without_posting(fixture["text"])
        self.assertEqual(draft["syntax_shape_valid"], fixture["syntax_shape_valid"])
        self.assertEqual(draft["diagnostics"], fixture["diagnostics"])
        request = self._captured(fixture["text"])
        self.assertEqual(request.get("engineering_error"), fixture["diagnostics"][0]["message"])

    def test_controller_metadata_injected_fixture_blocking_parity_with_capture(self):
        # (g) the controller-metadata-injected fixture: the validator's diagnostic and capture's
        # engineering_error are both exactly the refuse_model_metadata message.
        fixture = _FIXTURES_BY_NAME["controller-metadata-injected.json"]
        draft = self._validated_without_posting(fixture["text"])
        self.assertEqual(draft["syntax_shape_valid"], fixture["syntax_shape_valid"])
        self.assertEqual(draft["diagnostics"], fixture["diagnostics"])
        request = self._captured(fixture["text"])
        self.assertEqual(request.get("engineering_error"), fixture["diagnostics"][0]["message"])


class UndeliveredCompactContractParityTests(LifecycleFixture):
    """(h) Undelivered compact contract: the historical-session simulation pattern already used by
    test_ao_evidence_closure.py's RetainedSessionDeliveryTests, excluding ao_report_contract.PART
    (efficiency_contract_v1) from ao_workflow.PARTS for the room's entire bootstrap -- open, spec,
    bind, agree, handoff, the first implementation send, its finish and its sync -- so the compact
    contract part is never recorded in any completed request's carried parts for this native session.
    The offline validator (run with no room at all) reports the same draft valid and always names
    compact_contract_delivery among its unperformed_checks regardless; only the real
    ao_report_contract.project, reached through authoritative capture with authenticated room state,
    can see that the part was never actually delivered."""

    def test_capture_fails_when_the_compact_contract_was_never_delivered(self):
        reduced_parts = tuple(part for part in ao_workflow.PARTS if part != ao_report_contract.PART)
        self.assertIn(ao_report_contract.PART, ao_workflow.PARTS)
        self.assertNotIn(ao_report_contract.PART, reduced_parts)
        compact_text = _FIXTURES_BY_NAME["compact-valid.json"]["text"]
        with patch.object(ao_workflow, "PARTS", reduced_parts):
            self.room = self.open()
            self.spec()
            self.bind()
            self.agree()
            self.service.ao_room_handoff(self.room, str(self.repo))
            self.send("implementation")
            self.fake.finish("engineer", compact_text)
            self.service.ao_room_sync(self.room)
        request = self.state()["requests"]["implementation"]
        self.assertEqual(request.get("engineering_error"),
                          "Compact report contract was not delivered to this native session")

        posts_before = len(self.fake.posts)
        draft = ao_report_validation.validate_draft(compact_text)
        self.assertEqual(len(self.fake.posts), posts_before)  # the validator call itself never posts
        self.assertTrue(draft["syntax_shape_valid"], draft)
        self.assertIn("compact_contract_delivery", [item["check"] for item in draft["unperformed_checks"]])


class ZeroEffectsTests(unittest.TestCase):
    """ao_report_validate must never write, create a directory/config, touch sqlite3, or call out."""

    def test_service_call_on_a_bare_unprovisioned_service_object(self):
        service = object.__new__(project_room.Service)  # no __init__, no .home at all
        self.assertFalse(hasattr(service, "home"))
        result = service.call("ao_report_validate", {"report_text": json.dumps(full_report())})
        self.assertTrue(result["syntax_shape_valid"])
        self.assertFalse(hasattr(service, "home"))

    def test_service_call_on_the_bare_double_raises_the_same_depth_refusal_for_a_recursion_depth(self):
        # R1: the recursion-depth admission refusal propagates through Service.call exactly like any
        # other RoomError this read-only tool can raise; the bare object.__new__ double proves it
        # never needed self.home to do so.
        service = object.__new__(project_room.Service)
        self.assertFalse(hasattr(service, "home"))
        text = '{"a":' * 2000 + '1' + '}' * 2000
        with self.assertRaisesRegex(RoomError, "Draft report exceeds the admitted nesting depth"):
            service.call("ao_report_validate", {"report_text": text})
        self.assertFalse(hasattr(service, "home"))

    def test_mcp_tools_list_and_tools_call_never_touch_an_unprovisioned_home(self):
        """The double below reproduces exactly the instance state project_room.Service.__init__ would
        leave behind minus provisioning itself: self.home (a path that is never created), the empty
        self._progress_cache, self._legacy_tools_visible_cache left None (its own pre-provisioning
        value, not pre-seeded), and self.process_inspector, all without ever calling __init__ or
        creating home. Service.legacy_tools_visible() only opens home/ao/config.json inside a
        blanket try/except that treats any failure (including a missing file, via
        ao_project_room.read's plain json.loads(path.read_text())) as "visible"; it opens the sqlite
        registry only when that config selects the ao backend without legacy_tools, which cannot
        happen when the file does not exist. ao_project_room.sync_wait_max_seconds -- used by
        project_room_mcp._listed_schema for the ao_room_sync schema entry listed alongside this tool
        -- likewise treats a missing home/ao/config.json as its 45-second default. Neither reads
        reach for a write, mkdir or sqlite connection on a missing path. The one filesystem boundary
        deliberately out of scope here is ordinary MCP process startup, which constructs a real
        Service() (and so provisions a home) before this handler ever runs; that happens once, prior
        to and outside every call this test drives."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "unprovisioned-home"  # deliberately never created; isolated temp dir, not the repo
            self.assertFalse(home.exists())
            service = object.__new__(project_room.Service)
            service.home = home
            service._progress_cache = {}
            service._legacy_tools_visible_cache = None
            service.process_inspector = None

            listing = project_room_mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                                              service)
            tools = {tool["name"]: tool for tool in listing["result"]["tools"]}
            self.assertIn("ao_report_validate", tools)
            entry = tools["ao_report_validate"]
            self.assertEqual(entry["annotations"],
                              {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False})
            self.assertEqual(entry["inputSchema"],
                              {"type": "object", "properties": {"report_text": {"type": "string"}},
                               "required": ["report_text"], "additionalProperties": False})
            self.assertFalse(home.exists())

            call = project_room_mcp.handle(
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "ao_report_validate", "arguments": {"report_text": json.dumps(full_report())}}},
                service)
            self.assertFalse(call["result"]["isError"], call)
            self.assertTrue(call["result"]["structuredContent"]["syntax_shape_valid"])
            self.assertFalse(home.exists())

            shape_invalid_call = project_room_mcp.handle(
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": "ao_report_validate", "arguments": {"report_text": '{"outcome": "bogus"}'}}},
                service)
            self.assertFalse(shape_invalid_call["result"]["isError"], shape_invalid_call)
            self.assertFalse(shape_invalid_call["result"]["structuredContent"]["syntax_shape_valid"])
            self.assertFalse(home.exists())

            admission_refusal_call = project_room_mcp.handle(
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                 "params": {"name": "ao_report_validate", "arguments": {"report_text": ""}}},
                service)
            self.assertTrue(admission_refusal_call["result"]["isError"], admission_refusal_call)
            self.assertFalse(home.exists())

            # The direct Service.call proof, on this same more-realistic double (a set but
            # never-created home, not merely an object missing the attribute outright).
            direct = service.call("ao_report_validate", {"report_text": json.dumps(full_report())})
            self.assertTrue(direct["syntax_shape_valid"])
            self.assertFalse(home.exists())

            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_cli_call_skips_service_construction_and_creates_no_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "cli-home"  # deliberately never created; isolated temp dir, not the repo
            self.assertFalse(home.exists())
            args = json.dumps({"report_text": json.dumps(full_report())})
            completed = subprocess.run(
                [sys.executable, str(REPO_ROOT / "project_room.py"), "--home", str(home),
                 "call", "ao_report_validate", "--args", args],
                capture_output=True, text=True, cwd=str(REPO_ROOT))
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(completed.stdout)
            self.assertTrue(result["syntax_shape_valid"])
            self.assertEqual(completed.stderr, "")
            self.assertFalse(home.exists())

    def test_cli_invalid_arguments_report_the_shared_error_convention_and_still_create_no_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "cli-error-home"  # deliberately never created; isolated temp dir, not the repo
            self.assertFalse(home.exists())
            completed = subprocess.run(
                [sys.executable, str(REPO_ROOT / "project_room.py"), "--home", str(home),
                 "call", "ao_report_validate", "--args", "{}"],
                capture_output=True, text=True, cwd=str(REPO_ROOT))
            self.assertEqual(completed.returncode, 2)
            self.assertEqual(completed.stdout, "")
            self.assertEqual(json.loads(completed.stderr),
                              {"error": "Invalid arguments; missing=['report_text'], unexpected=[]"})
            self.assertFalse(home.exists())

    def test_cli_recursion_depth_refusal_reports_exit_2_and_still_creates_no_home(self):
        # R1: the CLI preflight dispatches ao_report_validate before Service construction (see
        # project_room.py main()), so a draft nested past the interpreter's own recursion limit must
        # still report the documented JSON-error/exit-2 admission refusal, not an unhandled traceback,
        # and must still never provision --home.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "cli-depth-home"  # deliberately never created; isolated temp dir, not the repo
            self.assertFalse(home.exists())
            text = '{"a":' * 2000 + '1' + '}' * 2000
            args = json.dumps({"report_text": text})
            completed = subprocess.run(
                [sys.executable, str(REPO_ROOT / "project_room.py"), "--home", str(home),
                 "call", "ao_report_validate", "--args", args],
                capture_output=True, text=True, cwd=str(REPO_ROOT))
            self.assertEqual(completed.returncode, 2, completed.stdout)
            self.assertEqual(completed.stdout, "")
            self.assertEqual(json.loads(completed.stderr),
                              {"error": "Draft report exceeds the admitted nesting depth"})
            self.assertFalse(home.exists())

    def test_no_network_process_database_or_filesystem_access(self):
        """Tripwire every provider/process/db/file/dir/socket primitive Service.__init__ or a delegate
        call would use, then run validate_draft over clean, malformed and unparsable text; none may
        fire. io.open and os.open are the lower-level primitives builtins.open and the directory
        helpers are themselves built on; socket.socket is the raw primitive under every provider or
        network client, independent of http.client's own connection classes above."""
        import builtins
        import io
        import os
        import socket

        class Tripwire(Exception):
            pass

        def trip(*_args, **_kwargs):
            raise Tripwire("network/process/db/file/dir access attempted")

        originals = {
            (http.client, "HTTPSConnection__init__"): http.client.HTTPSConnection.__init__,
            (http.client, "HTTPConnection__init__"): http.client.HTTPConnection.__init__,
            (subprocess.Popen, "__init__"): subprocess.Popen.__init__,
            (subprocess, "run"): subprocess.run,
            (sqlite3, "connect"): sqlite3.connect,
            (builtins, "open"): builtins.open,
            (io, "open"): io.open,
            (os, "open"): os.open,
            (os, "mkdir"): os.mkdir,
            (os, "makedirs"): os.makedirs,
            (Path, "mkdir"): Path.mkdir,
            (socket, "socket"): socket.socket,
        }
        http.client.HTTPSConnection.__init__ = trip
        http.client.HTTPConnection.__init__ = trip
        subprocess.Popen.__init__ = trip
        subprocess.run = trip
        sqlite3.connect = trip
        builtins.open = trip
        io.open = trip
        os.open = trip
        os.mkdir = trip
        os.makedirs = trip
        Path.mkdir = trip
        socket.socket = trip
        try:
            clean = ao_report_validation.validate_draft(json.dumps(full_report()))
            self.assertTrue(clean["syntax_shape_valid"])
            malformed = ao_report_validation.validate_draft('{"outcome": "bogus"}')
            self.assertFalse(malformed["syntax_shape_valid"])
            unparsable = ao_report_validation.validate_draft("not json at all {{{")
            self.assertFalse(unparsable["syntax_shape_valid"])
            with self.assertRaises(RoomError):
                ao_report_validation.validate_draft("")
        finally:
            http.client.HTTPSConnection.__init__ = originals[(http.client, "HTTPSConnection__init__")]
            http.client.HTTPConnection.__init__ = originals[(http.client, "HTTPConnection__init__")]
            subprocess.Popen.__init__ = originals[(subprocess.Popen, "__init__")]
            subprocess.run = originals[(subprocess, "run")]
            sqlite3.connect = originals[(sqlite3, "connect")]
            builtins.open = originals[(builtins, "open")]
            io.open = originals[(io, "open")]
            os.open = originals[(os, "open")]
            os.mkdir = originals[(os, "mkdir")]
            os.makedirs = originals[(os, "makedirs")]
            Path.mkdir = originals[(Path, "mkdir")]
            socket.socket = originals[(socket, "socket")]


class ToolSchemaTests(unittest.TestCase):
    """The tool is registered exactly once, with a bounded string-only schema, read-only but not openWorld."""

    def test_schema_requires_only_report_text(self):
        description, schema = ao_project_room.TOOL_SCHEMAS["ao_report_validate"]
        self.assertIsInstance(description, str)
        self.assertEqual(schema, {"type": "object", "properties": {"report_text": {"type": "string"}},
                                   "required": ["report_text"], "additionalProperties": False})

    def test_merged_into_project_room_tool_schemas(self):
        self.assertIn("ao_report_validate", project_room.TOOL_SCHEMAS)
        self.assertIs(project_room.TOOL_SCHEMAS["ao_report_validate"], ao_project_room.TOOL_SCHEMAS["ao_report_validate"])

    def test_cli_lists_the_tool_as_a_call_choice(self):
        completed = subprocess.run([sys.executable, str(REPO_ROOT / "project_room.py"), "call", "--help"],
                                   capture_output=True, text=True, cwd=str(REPO_ROOT))
        self.assertIn("ao_report_validate", completed.stdout)


if __name__ == "__main__":
    unittest.main()
