"""Synthetic coverage for the one explicitly authorized exact-spec re-review (ao_spec_rereview).

Fixture: three agreed charter reviews, a scope-change completion, the consumed pre-acceptance
extension's fourth review finished with a retained negative or noncanonical verdict, then the
operator-registered corrected next revision. Every negative refuses before any POST/consumption
and leaves every room byte unchanged; no grant is ever a second allowance, a refund or a renewal.
"""

import copy
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import threading
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import ao_project_room as ao
import ao_spec_review_budget as budget
import ao_spec_rereview as rereview
import ao_workflow
import project_room
import project_room_mcp
import project_room_runtime as runtime
from test_ao_preacceptance_review_extension import PreAcceptanceFixture
from test_ao_review_extension import ExecutableRuntimeMixin, ReviewExtensionFixture

EXHAUSTED = 'additional Fable review intent is exhausted'
REREVIEW_EXHAUSTED = 'one re-review intent is exhausted'


class ReReviewFixture(PreAcceptanceFixture):
    """The consumed charter extension's fourth review completed with the noncanonical negative
    verdict; the operator then registered the corrected revision-3 charter whose first gate
    declares a synthetic artifact path plus its bytes hash, exactly as an audited gate does.

    The counting shape is the retained one (as in Han's room): two accepting reviews, one
    non-accepting review and the extension consumed as the fourth dispatch, so the re-review is
    the fifth. The extension grant is minted under the retired completed-review eligibility --
    the strict debate budget admits this shape's audit only with three non-accepting reviews,
    while a retained grant minted under the old rule must still validate. The audit and extend
    calls bind real evidence; only the eligibility counter is simulated, and every later
    validation runs the real counters."""

    MESSAGE = 'Review the exact corrected specification revision 3.'
    REREVIEW_ID = 'spec-rereview'

    TEMPLATE_ATTRS = PreAcceptanceFixture.TEMPLATE_ATTRS + (
        'spec2', 'extension_grant', 'spec3', 'script', 'script_sha256', 'inputs')

    def build_template(self):
        super().build_template()
        self.through_fourth()
        self.spec3 = self.register_corrected()

    def through_fourth(self):
        self.agree(decision='changes_required', key='charter-2')
        self.agree(key='charter-3')
        self.propose_scope_change()
        self.spec2 = self.register()
        self.extension_grant = self.legacy_grant()
        self.send('spec_review', 'fourth')
        self.fake.finish('engineer', json.dumps(self.source_verdict()))
        self.service.ao_room_sync(self.room)

    def register(self):
        if self.target_spec is None:
            self.target_spec = self.service.ao_room_spec_put(self.room, 2,
                'Review the newly authorized validation charter after the proposed scope change.',
                self.gates, 'Astra approves this exact next charter under the actual scope decision')
        return self.target_spec

    def audit(self, **changes):
        changes.setdefault('spec_revision', 2)
        return super().audit(**changes)

    def legacy_grant(self):
        """Mint the one extension under the retained completed-review eligibility.

        Three completed reviews stand where the strict debate budget sees only one
        non-accepting review, exactly the position of a retained room whose grant predates
        debate counting. The patched counter covers only the audit and grant calls; the bound
        evidence, the consumption journal and every subsequent validation are real.
        """
        with patch.object(budget, 'debate_attempts', lambda directory, state: budget.DEBATE_LIMIT):
            self.inputs = self.grant_inputs()
            return self.service.ao_room_preacceptance_review_extend(**self.inputs)

    def source_verdict(self):
        """The retained noncanonical result: one JSON object, no canonical ``decision``."""
        return {'report_format': 'project_room_spec_verdict',
                'review': 'The registered charter contradicts the retained scope requirement.',
                'spec_revision': self.spec2['revision'], 'spec_sha256': self.spec2['sha256'],
                'contract_note': 'strict verdict contract absent after compaction',
                'verdict': 'disagree',
                'verdict_basis': 'The corrected gate omits the retained scope check.',
                'blockers': [{'id': 'B1', 'severity': 'BLOCKER',
                              'finding': 'The scope gate does not exercise the corrected path.',
                              'required_correction': 'Add the scope-check gate to the corrected charter.'}],
                'findings': [{'id': 'F1', 'severity': 'non_blocking',
                              'finding': 'The charter prose is clear.'}],
                'operator_requests': [], 'routing_log': []}

    def write_script(self):
        self.script = self.repo / 'scope_gate.py'
        self.script.write_text("from pathlib import Path\nassert Path('feature.txt').is_file()\n")
        self.script_sha256 = ao.digest(self.script.read_bytes())
        return self.script

    def corrected_gates(self):
        return [[sys.executable, '-B', str(self.script), self.script_sha256]] + [list(g) for g in self.gates]

    def register_corrected(self):
        self.write_script()
        return self.service.ao_room_spec_put(self.room, 3,
            'Review the newly authorized corrected validation charter after the re-review decision.',
            self.corrected_gates(),
            'Astra approves this exact corrected charter under the actual re-review decision')

    def audit_inputs(self, **changes):
        source = self.state()['requests'].get('fourth')
        raw = ao_workflow.raw_final_text(self.directory(), source) if source else ''
        inputs = {'room_id': self.room, 'spec_revision': 3, 'spec_sha256': self.spec3['sha256'],
                  'source_request_id': 'fourth',
                  'source_receipt_sha256': (source or {}).get('receipt_sha256', '0' * 64),
                  'source_final_text_sha256': ao.digest(raw.encode()),
                  'source_classification': 'noncanonical_negative',
                  'request_id': self.REREVIEW_ID,
                  'message_sha256': ao.digest(self.MESSAGE.encode()),
                  'gate_artifacts': [str(self.script)],
                  'native_session_id': self.NATIVE, 'native_owner_database': str(self.database)}
        inputs.update(changes)
        return inputs

    def rereview_audit(self, **changes):
        return self.service.ao_room_spec_rereview_audit(**self.audit_inputs(**changes))

    def rereview_grant(self, audit=None, **changes):
        inputs = {'room_id': self.room, 'audit_sha256': (audit or self.rereview_audit())['audit_sha256'],
                  'authorization': ('User answered "yes, one more review" to the explicit proposal for a single '
                                    'authorized re-review of the corrected charter, in this retained room and session.'),
                  'authorization_reference': 'The retained operator answer recorded alongside this room decision.',
                  'diagnosis': ('The consumed fourth review returned a retained negative verdict on the prior '
                                'charter; the corrected charter needs one exact review.')}
        inputs.update(changes)
        return self.service.ao_room_spec_rereview_grant(**inputs)

    def send_rereview(self, request_id=None, message=None):
        key = request_id or self.REREVIEW_ID
        result = self.service.ao_room_send(self.room, 'engineer', message or self.MESSAGE, key,
                                           purpose='spec_review')
        self.note_native_turn(key)
        return result

    def finish_rereview(self, **verdict):
        value = {'interpretation': 'Exact corrected charter only.', 'findings': [],
                 'decision': 'accept', 'spec_revision': 3, 'spec_sha256': self.spec3['sha256'], **verdict}
        self.fake.finish('engineer', json.dumps(value))
        return self.service.ao_room_sync(self.room)

    def granted(self):
        audited = self.rereview_audit()
        return audited, self.rereview_grant(audit=audited)


class ModuleDiscoveryTests(unittest.TestCase):
    """No foreign ``TestCase`` class is collected under this module's own name.

    Importing a TestCase class for reuse (the pre-mixin ``configure_runtime`` source) makes
    ``loadTestsFromNames`` collect that class's tests a second time under this module.
    """

    def test_every_collected_test_is_defined_in_this_module(self):
        suite = unittest.TestLoader().loadTestsFromName('test_ao_spec_rereview')
        stack, foreign = [suite], []
        while stack:
            item = stack.pop()
            if isinstance(item, unittest.TestSuite):
                stack.extend(item)
            elif item.__class__.__module__ != 'test_ao_spec_rereview':
                foreign.append(item.id())
        self.assertEqual(sorted(foreign), [])


class ReReviewBaselineTests(ReReviewFixture):
    """Baseline: without a re-review pointer the old admission path is byte-identical."""

    def test_consumed_fourth_still_exhausts_ordinary_reviews_and_no_agreement(self):
        status = self.service.ao_room_status(self.room)
        self.assertEqual(status['spec_review_attempts'], 4)
        self.assertFalse(status['agreement']['agreed'])
        self.assertIsNone(status['spec_rereview'])
        self.assertEqual(status['spec_review_extension']['consumed_by'], 'fourth')
        # The corrected revision is current: ordinary agreement refuses on the spec identity
        # before it can even read the retained noncanonical verdict.
        self.assertIn('requires completed Fable agreement', status['agreement']['reason'])
        with self.assertRaisesRegex(ao.RoomError, 'requires completed Fable agreement'):
            ao_workflow.agreement(self.service, self.directory(), self.state())
        # The source result is exactly one JSON object and has no canonical decision.
        fourth = self.state()['requests']['fourth']
        verdict = ao_workflow.final_json(self.directory(), fourth)
        self.assertEqual(verdict['verdict'], 'disagree')
        self.assertNotIn('decision', verdict)
        self.assertEqual(verdict['blockers'][0]['severity'], 'BLOCKER')
        self.assert_refuses_without_mutation(EXHAUSTED,
                                             lambda: self.send('spec_review', 'fifth'))
        self.assert_refuses_without_mutation(EXHAUSTED,
                                             lambda: self.service.ao_room_send(
                                                 self.room, 'engineer', self.MESSAGE, self.REREVIEW_ID,
                                                 purpose='spec_review'))
        self.assertEqual(len(self.fake.posts), 5)


class ReReviewAuditTests(ReReviewFixture):
    """Audit eligibility, mechanical classification and every audited-input refusal."""

    def test_audit_is_eligible_binds_evidence_and_mutates_nothing(self):
        before = (self.directory() / 'state.json').read_bytes()
        posts = len(self.fake.posts)
        audited = self.rereview_audit()
        self.assertTrue(audited['eligible'])
        self.assertEqual(audited['source_result_kind'], 'noncanonical_object')
        self.assertEqual(audited['spec_record_sha256'], self.state()['spec_record_sha256'])
        self.assertEqual(audited['gate_artifacts'], {str(self.script): self.script_sha256})
        self.assertRegex(audited['review_contract_sha256'], '^[0-9a-f]{64}$')
        self.assertFalse(audited['model_dispatch'])
        self.assertEqual((self.directory() / 'state.json').read_bytes(), before)
        self.assertEqual(len(self.fake.posts), posts)
        saved = rereview._audit(self.directory(), audited['audit_sha256'])
        self.assertEqual(saved['version'], 1)
        self.assertEqual(saved['evidence']['extension']['consumer_request_id'], 'fourth')
        self.assertEqual(saved['evidence']['extension']['lane'], 'pre_acceptance')
        self.assertFalse(saved['evidence']['source']['canonical_decision_present'])
        # An audit alone is never an allowance.
        self.assert_refuses_without_mutation(EXHAUSTED, lambda: self.send_rereview())

    def test_classification_inputs_and_source_identity_are_exact(self):
        for changes in ({'source_classification': 'changes_required'},
                        {'source_classification': 'not-a-kind'},
                        {'source_request_id': 'charter-3'},
                        {'source_request_id': 'implementation'},
                        {'source_request_id': 'never-sent'},
                        {'source_receipt_sha256': '0' * 64},
                        {'source_final_text_sha256': '0' * 64},
                        {'request_id': 'charter-1'},
                        {'spec_revision': 2}, {'spec_revision': 4}, {'spec_sha256': '0' * 64},
                        {'native_session_id': 'another-native-owner'},
                        {'native_owner_database': str(self.root / 'other.db')}):
            with self.subTest(changed=changes):
                self.assert_refuses_without_mutation('.+', lambda c=changes: self.rereview_audit(**c))

    def test_corrected_revision_must_be_exactly_the_next_registered_charter(self):
        audited = self.rereview_audit()
        self.assertTrue(audited['eligible'])
        self.service.ao_room_spec_put(self.room, 4, 'Another corrected charter.', self.gates,
                                      'Astra approves this further corrected charter')
        self.assert_refuses_without_mutation('exact corrected next charter',
                                             lambda: self.rereview_audit())

    def test_gate_artifact_rules_refuse(self):
        # Declared path absent from every registered gate argv.
        stray = self.repo / 'stray_gate.py'
        stray.write_text('pass\n')
        self.assert_refuses_without_mutation('absent from every registered gate argv',
            lambda: self.rereview_audit(gate_artifacts=[str(stray)]))
        self.assert_refuses_without_mutation('list of absolute paths',
            lambda: self.rereview_audit(gate_artifacts=['relative/path.py']))


class ReReviewMismatchedHashFixture(ReReviewFixture):
    def corrected_gates(self):
        return [[sys.executable, '-B', str(self.script), '0' * 64]] + [list(g) for g in self.gates]


class ReReviewMismatchedHashTests(ReReviewMismatchedHashFixture):
    def test_declared_gate_hash_differs_from_bytes(self):
        self.assert_refuses_without_mutation('declared hash differs from its bytes',
                                             lambda: self.rereview_audit())


class ReReviewAbsentArtifactFixture(ReReviewFixture):
    def corrected_gates(self):
        return [list(g) for g in self.gates]


class ReReviewAbsentArtifactTests(ReReviewAbsentArtifactFixture):
    def test_artifact_absent_from_every_gate_argv(self):
        self.assert_refuses_without_mutation('absent from every registered gate argv',
                                             lambda: self.rereview_audit())


class ReReviewSymlinkArtifactFixture(ReReviewFixture):
    def corrected_gates(self):
        linked = self.repo / 'linked_gate.py'
        os.symlink(self.script, linked)
        self.script = linked
        return [[sys.executable, '-B', str(linked), self.script_sha256]] + [list(g) for g in self.gates]


class ReReviewSymlinkArtifactTests(ReReviewSymlinkArtifactFixture):
    def test_symlinked_artifact_refuses(self):
        self.assert_refuses_without_mutation('regular file without links',
                                             lambda: self.rereview_audit())


class ReReviewGrantTests(ReReviewFixture):
    def test_grant_commits_one_named_intent_and_replays_idempotently(self):
        audited, granted = self.granted()
        pointer = self.state()['spec_rereview']
        self.assertEqual(pointer['request_id'], self.REREVIEW_ID)
        self.assertEqual(pointer['receipt'], 'spec-rereview/requests/spec-rereview.json')
        self.assertEqual(granted['remaining_spec_reviews'], 1)
        self.assertEqual(granted['consumed_by'], None)
        self.assertEqual(granted['lane'], 'spec_rereview')
        self.assertFalse(granted['model_dispatch'])
        same = self.service.ao_room_spec_rereview_grant(room_id=self.room, audit_sha256=audited['audit_sha256'],
            authorization=('User answered "yes, one more review" to the explicit proposal for a single '
                           'authorized re-review of the corrected charter, in this retained room and session.'),
            authorization_reference='The retained operator answer recorded alongside this room decision.',
            diagnosis=('The consumed fourth review returned a retained negative verdict on the prior '
                       'charter; the corrected charter needs one exact review.'))
        self.assertEqual(same['receipt_sha256'], granted['receipt_sha256'])
        status = self.service.ao_room_status(self.room)['spec_rereview']
        self.assertEqual(status['status'], 'granted')
        self.assertEqual(status['remaining_spec_reviews'], 1)

    def test_second_grant_or_different_payload_refuses(self):
        audited, _ = self.granted()
        self.assert_refuses_without_mutation('another payload',
            lambda: self.service.ao_room_spec_rereview_grant(
                room_id=self.room, audit_sha256=audited['audit_sha256'],
                authorization='A different answer entirely.', authorization_reference='Somewhere else.',
                diagnosis='Different.'))
        self.assert_refuses_without_mutation('Only one re-review continuation',
                                             lambda: self.rereview_audit())

    def test_stale_audit_refuses_the_grant(self):
        audited = self.rereview_audit()
        (self.repo / 'drift.txt').write_text('changed after audit\n')
        self.assert_refuses_without_mutation('audit is stale',
            lambda: self.service.ao_room_spec_rereview_grant(
                room_id=self.room, audit_sha256=audited['audit_sha256'],
                authorization='Actual answer.', authorization_reference='Provenance.', diagnosis='Diagnosed.'))

    def test_grant_requires_all_three_authority_fields(self):
        audited = self.rereview_audit()
        for field in ('authorization', 'authorization_reference', 'diagnosis'):
            with self.subTest(missing=field):
                self.assert_refuses_without_mutation(field,
                    lambda f=field: self.rereview_grant(audit=audited, **{f: ''}))


class ReReviewSendTests(ReReviewFixture):
    """The authorized send: exactly one POST, review_contract redelivered once, old bytes intact."""

    def test_authorized_rereview_send_completes_and_agrees_only_afterwards(self):
        original_files = self.room_files()
        _, granted = self.granted()
        self.assert_refuses_without_mutation('requires completed Fable agreement',
            lambda: ao_workflow.agreement(self.service, self.directory(), self.state()))
        self.send_rereview()
        self.assertEqual(len(self.fake.posts), 6)
        request = self.state()['requests'][self.REREVIEW_ID]
        carried = request['carried']
        self.assertEqual(carried['spec_rereview_sha256'], granted['receipt_sha256'])
        self.assertEqual(carried['parts'], ['review_contract'])
        audited = rereview._audit(self.directory(),
            ao.read(self.directory() / 'spec-rereview/requests/spec-rereview.json')['inputs']['audit_sha256'])
        self.assertEqual(carried['part_sha256']['review_contract'],
                         audited['evidence']['review_contract_sha256'])
        prepared = ao.read(self.directory() / self.state()['preparation'])
        import ao_delegates
        policy = ao_delegates.validate_provider(self.directory(), self.state())
        contract = ao_workflow.part_texts(prepared, policy)['review_contract']
        self.assertIn(contract, request['text'])
        self.assertIn('revision 3', request['text'])
        self.assertTrue(request.get('spec_rereview_consumption'))
        self.assertEqual(len(rereview._consumptions(self.directory())), 1)
        self.assertEqual(self.service.ao_room_status(self.room)['spec_rereview']['status'], 'consumed')
        # The granted grant does not reinterpret the source result; agreement still refuses until the verdict.
        self.assertFalse(self.service.ao_room_status(self.room)['agreement']['agreed'])
        status = self.finish_rereview()
        self.assertTrue(status['agreement']['agreed'])
        self.assertEqual(self.service.ao_room_status(self.room)['spec_review_attempts'], 5)
        self.assertEqual(self.service.ao_room_status(self.room)['spec_review_extension']['consumed_by'], 'fourth')
        handoff = self.service.ao_room_handoff(self.room, str(self.repo))
        self.assertEqual(handoff['spec_revision'], 3)
        sent_impl = self.service.ao_room_send(self.room, 'engineer', 'Implement the exact authorized purpose.',
                                              'implementation-after-rereview', purpose='implementation')
        self.assertEqual(sent_impl['state'], 'submitted')
        # Every earlier request's retained text and receipt bytes are unchanged by the new intent.
        for request_id in ('charter-1', 'charter-2', 'charter-3', 'implementation', 'fourth'):
            for key in ('text', 'receipt', 'receipt_sha256', 'message', 'carried'):
                self.assertEqual(self.state()['requests'][request_id].get(key),
                                 ao.read(self.directory() / 'state.json')['requests'][request_id].get(key))
        for path, content in original_files.items():
            if path == 'state.json' or path.startswith('spec-rereview/'):
                continue
            self.assertEqual((self.directory() / path).read_bytes(), content)

    def test_review_contract_redelivery_delivers_no_other_part_twice(self):
        self.granted()
        before = {key: self.state()['requests'][key]['text']
                  for key in ('charter-1', 'charter-2', 'charter-3', 'fourth')}
        self.send_rereview()
        request = self.state()['requests'][self.REREVIEW_ID]
        self.assertEqual(request['carried']['parts'], ['review_contract'])
        import ao_delegates
        prepared = ao.read(self.directory() / self.state()['preparation'])
        policy = ao_delegates.validate_provider(self.directory(), self.state())
        for name, text in ao_workflow.part_texts(prepared, policy).items():
            if name == 'review_contract':
                self.assertIn(text, request['text'])
            else:
                self.assertNotIn(text, request['text'])
        for key, text in before.items():
            self.assertEqual(self.state()['requests'][key]['text'], text)
        delivered = ao_workflow.delivered(self.state(), self.state()['bindings']['engineer']['session_id'])
        self.assertIn('review_contract', delivered['parts'])

    def test_exhausted_rereview_refuses_any_further_spec_review(self):
        self.granted()
        self.send_rereview()
        self.finish_rereview()
        self.assert_refuses_without_mutation(REREVIEW_EXHAUSTED,
            lambda: self.service.ao_room_send(self.room, 'engineer', self.MESSAGE, 'another-review',
                                              purpose='spec_review'))

    def test_negative_and_blocker_rereview_results_never_agree(self):
        self.granted()
        self.send_rereview()
        self.finish_rereview(decision='changes_required', interpretation='Still wrong.',
                             findings=['BLOCKER: Gate absent.'])
        status = self.service.ao_room_status(self.room)
        self.assertFalse(status['agreement']['agreed'])
        with self.assertRaisesRegex(ao.RoomError, 'did not accept the exact current spec|rejected'):
            self.service.ao_room_handoff(self.room, str(self.repo))


class ReReviewBlockerFinishFixture(ReReviewFixture):
    """The authorized re-review accepts but carries a blocker finding: agreement stays refused."""

    def finish_rereview(self, **verdict):
        return super().finish_rereview(findings=['BLOCKER: Residual.'], **verdict)


class ReReviewBlockerFinishTests(ReReviewBlockerFinishFixture):
    def test_accept_with_a_blocker_finding_never_agrees(self):
        self.granted()
        self.send_rereview()
        self.finish_rereview()
        self.assertFalse(self.service.ao_room_status(self.room)['agreement']['agreed'])


class ReReviewNoncanonicalFinishFixture(ReReviewFixture):
    """The authorized re-review itself returns a noncanonical object: never agreement."""

    def finish_rereview(self, **verdict):
        self.fake.finish('engineer', json.dumps(self.source_verdict()))
        return self.service.ao_room_sync(self.room)


class ReReviewNoncanonicalFinishTests(ReReviewNoncanonicalFinishFixture):
    def test_noncanonical_rereview_result_never_agrees(self):
        self.granted()
        self.send_rereview()
        self.finish_rereview()
        status = self.service.ao_room_status(self.room)
        self.assertFalse(status['agreement']['agreed'])
        with self.assertRaisesRegex(ao.RoomError, 'did not accept the exact current spec|rejected'):
            self.service.ao_room_handoff(self.room, str(self.repo))


class ReReviewAdmissionRefusalTests(ReReviewFixture):
    """Each refusal happens after the grant but before any POST or consumption."""

    def test_different_message_refuses(self):
        self.granted()
        self.assert_refuses_without_mutation('audited exact bytes',
            lambda: self.send_rereview(message='A different caller message.'))

    def test_different_request_id_refuses(self):
        self.granted()
        self.assert_refuses_without_mutation('exact named re-review intent',
            lambda: self.send_rereview(request_id='not-the-grant'))
        self.assertEqual(len(rereview._consumptions(self.directory())), 0)

    def test_reused_existing_request_id_refuses(self):
        self.granted()
        self.assert_refuses_without_mutation('.+',
            lambda: self.send_rereview(request_id='charter-1'))

    def test_spec_put_of_a_different_revision_after_the_grant_refuses(self):
        self.granted()
        self.assert_refuses_without_mutation('unused re-review grant',
            lambda: self.service.ao_room_spec_put(self.room, 4, 'Yet another charter.', self.gates,
                                                  'Astra approves this other charter'))
        # The identical registration stays idempotent.
        again = self.service.ao_room_spec_put(self.room, 3,
            'Review the newly authorized corrected validation charter after the re-review decision.',
            self.corrected_gates(),
            'Astra approves this exact corrected charter under the actual re-review decision')
        self.assertEqual(again['sha256'], self.spec3['sha256'])

    def test_gate_script_modified_after_audit_refuses_admission(self):
        self.granted()
        self.script.write_text('assert False\n')
        self.assert_refuses_without_mutation('declared hash differs|gate artifact changed',
                                             lambda: self.send_rereview())

    def test_candidate_drift_refuses_admission(self):
        self.granted()
        (self.repo / 'drift.txt').write_text('candidate changed\n')
        self.assert_refuses_without_mutation('candidate .* changed after the grant|uncommitted changes',
                                             lambda: self.send_rereview())

    def test_owner_database_row_change_refuses_admission(self):
        self.granted()
        with sqlite3.connect(self.database) as db:
            db.execute('UPDATE sessions SET workspace_path=?', (str(self.root / 'moved-workspace'),))
        # The observation gate may durably retain its own evidence while refusing; still zero POSTs.
        posts = len(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError,
                'Native semantic hold|Native owner differs|retained native owner changed'):
            self.send_rereview()
        self.assertEqual(len(self.fake.posts), posts)
        # The lane's own recheck names the owner drift directly.
        with self.assertRaisesRegex(ao.RoomError, 'Native owner differs|retained native owner changed'):
            rereview.admission(self.service, self.directory(), self.state(), self.MESSAGE)

    def test_native_history_change_refuses_admission(self):
        self.granted()
        turns = self.fake.snapshots['engineer']['turns']
        turns.append(dict(turns[-1], id='synthetic-appended-turn'))
        self.assert_refuses_without_mutation(
            'Cannot attribute semantic outcome|Native history or metadata changed|active work|Native',
            lambda: self.send_rereview())
        with self.assertRaisesRegex(ao.RoomError, 'Native history or metadata changed|active work|Native'):
            rereview.admission(self.service, self.directory(), self.state(), self.MESSAGE)


class ReReviewUnconsumedFixture(ReReviewFixture):
    """The extension is granted but the fourth intent was deliberately never sent."""

    def through_fourth(self):
        self.agree(decision='changes_required', key='charter-2')
        self.agree(key='charter-3')
        self.propose_scope_change()
        self.spec2 = self.register()
        self.extension_grant = self.legacy_grant()

    def register_corrected(self):
        self.write_script()
        return {'revision': 3, 'sha256': '0' * 64}


class ReReviewUnconsumedTests(ReReviewUnconsumedFixture):
    def test_extension_not_consumed_refuses_the_audit(self):
        self.assert_refuses_without_mutation('consumed by exactly one completed additional review',
            lambda: self.rereview_audit(source_request_id='implementation',
                                        source_receipt_sha256='0' * 64,
                                        source_final_text_sha256='0' * 64))


class ReReviewProseSourceFixture(ReReviewFixture):
    """The fourth review finished as plain prose, never one JSON object."""

    def through_fourth(self):
        self.agree(decision='changes_required', key='charter-2')
        self.agree(key='charter-3')
        self.propose_scope_change()
        self.spec2 = self.register()
        self.extension_grant = self.legacy_grant()
        self.send('spec_review', 'fourth')
        self.fake.finish('engineer', 'The charter is refused in plain words.')
        self.service.ao_room_sync(self.room)


class ReReviewProseSourceTests(ReReviewProseSourceFixture):
    def test_prose_only_source_refuses_with_the_format_lane_message(self):
        self.assert_refuses_without_mutation('prose-only or multi-object source result',
            lambda: self.rereview_audit())


class ReReviewAcceptSourceFixture(ReReviewFixture):
    """The fourth review canonically accepted its charter: no re-review continuation applies."""

    MODE = 'accept'

    def source_verdict(self):
        if self.MODE == 'accept':
            return {'interpretation': 'Exact next charter only.', 'findings': [],
                    'decision': 'accept', 'spec_revision': 2, 'spec_sha256': self.spec2['sha256']}
        if self.MODE == 'accept_blockers':
            return {'interpretation': 'Accepted but a blocker remains.', 'findings': ['BLOCKER: Residual.'],
                    'decision': 'accept', 'spec_revision': 2, 'spec_sha256': self.spec2['sha256']}
        return {'interpretation': 'The charter omits the check.', 'findings': ['BLOCKER: Scope gate absent.'],
                'decision': 'changes_required', 'spec_revision': 2, 'spec_sha256': self.spec2['sha256']}


class ReReviewAcceptSourceTests(ReReviewAcceptSourceFixture):
    def test_canonical_accept_source_refuses(self):
        self.assert_refuses_without_mutation('accepted its specification',
            lambda: self.rereview_audit(source_classification='accept_with_blockers'))


class ReReviewAcceptBlockersFixture(ReReviewAcceptSourceFixture):
    MODE = 'accept_blockers'


class ReReviewAcceptBlockersTests(ReReviewAcceptBlockersFixture):
    def test_accept_with_blockers_source_admits_and_completes(self):
        audited = self.rereview_audit(source_classification='accept_with_blockers')
        self.assertEqual(audited['source_result_kind'], 'accept_with_blockers')
        self.rereview_grant(audit=audited)
        self.send_rereview()
        status = self.finish_rereview()
        self.assertTrue(status['agreement']['agreed'])


class ReReviewChangesRequiredFixture(ReReviewAcceptSourceFixture):
    MODE = 'changes_required'


class ReReviewChangesRequiredTests(ReReviewChangesRequiredFixture):
    def test_canonical_changes_required_source_admits_with_matching_classification(self):
        audited = self.rereview_audit(source_classification='changes_required')
        self.assertEqual(audited['source_result_kind'], 'changes_required')
        self.assert_refuses_without_mutation('declared source classification does not match',
            lambda: self.rereview_audit(source_classification='noncanonical_negative'))


class ReReviewTamperTests(ReReviewFixture):
    """Evidence tampering refuses validate/status/admission, always before any POST."""

    def test_missing_or_modified_grant_receipt_refuses(self):
        self.granted()
        receipt = self.directory() / 'spec-rereview/requests/spec-rereview.json'
        original = receipt.read_bytes()
        try:
            receipt.unlink()
            self.assert_refuses_without_mutation('single-grant identity changed|unreadable or inconsistent',
                                                 lambda: self.send_rereview())
            self.assertEqual(self.service.ao_room_status(self.room)['spec_rereview']['status'], 'damaged')
            receipt.write_bytes(original)
            receipt.write_text(json.dumps({**ao.read(receipt), 'version': 9}))
            self.assert_refuses_without_mutation('grant was modified|unreadable or inconsistent',
                                                 lambda: self.send_rereview())
        finally:
            receipt.write_bytes(original)

    def test_missing_or_modified_audit_record_refuses(self):
        audited, _ = self.granted()
        path = self.directory() / 'spec-rereview/audits' / (audited['audit_sha256'] + '.json')
        original = path.read_bytes()
        try:
            path.unlink()
            self.assert_refuses_without_mutation('unreadable or inconsistent',
                                                 lambda: self.send_rereview())
            path.write_bytes(original)
            record = ao.read(path); record['observed_at'] = 0.0
            ao.atomic(path, record)
            self.assert_refuses_without_mutation('audit was modified', lambda: self.send_rereview())
        finally:
            path.write_bytes(original)

    def test_missing_or_modified_consumption_refuses(self):
        self.granted()
        self.send_rereview()
        paths = rereview._consumptions(self.directory())
        self.assertEqual(len(paths), 1)
        path, original = paths[0], paths[0].read_bytes()
        try:
            path.unlink()
            self.assert_refuses_without_mutation('no exact matching one-time grant',
                                                 lambda: self.send_rereview(request_id='post-damage'))
            path.write_bytes(original)
            record = ao.read(path); record['version'] = 2
            ao.atomic(path, record)
            self.assert_refuses_without_mutation('consumption evidence was modified',
                lambda: self.send_rereview(request_id='post-damage-two'))
            self.assertEqual(self.service.ao_room_status(self.room)['spec_rereview']['status'], 'damaged')
        finally:
            path.write_bytes(original)

    def test_receipt_without_pointer_and_orphan_consumption_refuse(self):
        audited = self.rereview_audit()
        with patch.object(self.service, 'save', side_effect=OSError('synthetic grant projection interruption')):
            with self.assertRaises(OSError):
                self.rereview_grant(audit=audited)
        self.assertIsNone(self.state().get('spec_rereview'))
        self.assert_refuses_without_mutation('uncommitted re-review receipt',
                                             lambda: self.send_rereview())
        self.assertEqual(self.service.ao_room_status(self.room)['spec_rereview']['status'],
                         'pending_uncommitted')
        # An orphan consumption without any grant refuses even harder.
        pending = rereview._pending(self.directory())
        for path in pending:
            path.unlink()
        orphan = {'version': 1, 'room_id': self.room, 'grant_receipt_sha256': '0' * 64,
                  'request': {'request_id': 'ghost', 'state': 'uncertain', 'purpose': 'spec_review'}}
        rereview._store_once(self.directory() / 'spec-rereview/consumption' / (ao.digest(orphan) + '.json'), orphan)
        self.assert_refuses_without_mutation('no committed grant',
            lambda: self.send_rereview(request_id='after-orphan'))
        self.assertEqual(self.service.ao_room_status(self.room)['spec_rereview']['status'], 'damaged')

    def test_relabeled_or_reordered_claims_refuse(self):
        self.granted()
        self.send_rereview()
        directory = self.directory()
        state = self.state()
        for label, mutate in (
                ('removed', lambda s: s['requests'][self.REREVIEW_ID]['carried'].pop('spec_rereview_sha256')),
                ('moved', lambda s: (s['requests']['charter-3'].setdefault('carried', {}).update(
                    spec_rereview_sha256=s['requests'][self.REREVIEW_ID]['carried'].pop('spec_rereview_sha256')))),
                ('reordered', lambda s: s['requests'].__setitem__('fourth', {
                    **s['requests']['fourth'], 'created_order': s['requests'][self.REREVIEW_ID]['created_order'] + 1}))):
            with self.subTest(label=label):
                before = (directory / 'state.json').read_bytes()
                current = copy.deepcopy(state)
                mutate(current)
                ao.atomic(directory / 'state.json', current)
                try:
                    self.assert_refuses_without_mutation(
                        'no exact matching one-time grant|relabeled|attempts were removed'
                        '|prior requests|request identity differs',
                        lambda: self.send_rereview(request_id='post-damage'))
                finally:
                    (directory / 'state.json').write_bytes(before)

    def test_symlink_at_a_record_path_refuses(self):
        self.granted()
        receipt = self.directory() / 'spec-rereview/requests/spec-rereview.json'
        target = self.directory() / 'elsewhere.json'
        target.write_bytes(receipt.read_bytes())
        receipt.unlink()
        os.symlink(target, receipt)
        try:
            self.assert_refuses_without_mutation('unreadable or inconsistent|identity changed',
                                                 lambda: self.send_rereview())
        finally:
            receipt.unlink()
            receipt.write_bytes(target.read_bytes())
            target.unlink()


class ReReviewCrashBoundaryTests(ReReviewFixture):
    def test_grant_receipt_durable_across_state_projection_crash(self):
        audited = self.rereview_audit()
        inputs = {'room_id': self.room, 'audit_sha256': audited['audit_sha256'],
                  'authorization': 'Actual answer one.', 'authorization_reference': 'Provenance one.',
                  'diagnosis': 'Diagnosed one.'}
        before = (self.directory() / 'state.json').read_bytes()
        with patch.object(self.service, 'save', side_effect=OSError('synthetic grant projection interruption')):
            with self.assertRaises(OSError):
                self.service.ao_room_spec_rereview_grant(**inputs)
        self.assertEqual((self.directory() / 'state.json').read_bytes(), before)
        pending = rereview._pending(self.directory())
        self.assertEqual(len(pending), 1)
        receipt_bytes = pending[0].read_bytes()
        self.assert_refuses_without_mutation('uncommitted re-review receipt', lambda: self.send_rereview())
        self.assert_refuses_without_mutation('another payload',
            lambda: self.service.ao_room_spec_rereview_grant(**{**inputs, 'diagnosis': 'Different.'}))
        reconciled = self.service.ao_room_spec_rereview_grant(**inputs)
        self.assertEqual(reconciled['remaining_spec_reviews'], 1)
        self.assertEqual(pending[0].read_bytes(), receipt_bytes)
        self.assertEqual(self.state()['spec_rereview']['request_id'], self.REREVIEW_ID)

    def test_post_failure_after_projection_stays_durable_and_never_replays(self):
        """The POST itself fails after the durable consumption and the projected state."""
        self.granted()
        original_request = self.fake.request
        def failing(method, path, payload=None):
            if method == 'POST':
                raise OSError('synthetic dispatch failure after durable consumption')
            return original_request(method, path, payload)
        self.fake.request = failing
        try:
            sent = self.service.ao_room_send(self.room, 'engineer', self.MESSAGE, self.REREVIEW_ID,
                                             purpose='spec_review')
        finally:
            self.fake.request = original_request
        self.assertEqual(sent['state'], 'uncertain')
        request = self.state()['requests'][self.REREVIEW_ID]
        self.assertTrue(request.get('delivery_error'))
        self.assertEqual(len(rereview._consumptions(self.directory())), 1)
        posts = len(self.fake.posts)
        # The same request id returns its retained uncertain summary; it never POSTs again.
        same = self.send_rereview()
        self.assertEqual(same['state'], 'uncertain')
        self.assertEqual(len(self.fake.posts), posts)
        self.assertEqual(self.service.ao_room_status(self.room)['spec_rereview']['status'], 'consumed')
        self.assert_refuses_without_mutation('active or uncertain',
            lambda: self.send_rereview(request_id='different-review'))

    def test_lost_acknowledgement_is_uncertain_and_never_replayed(self):
        self.granted()
        self.fake.lose_ack = True
        sent = self.send_rereview()
        self.assertEqual(sent['state'], 'uncertain')
        posts = len(self.fake.posts)
        # The same request id returns its uncertain summary without another POST.
        same = self.send_rereview()
        self.assertEqual(same['state'], 'uncertain')
        self.assertEqual(len(self.fake.posts), posts)
        self.assert_refuses_without_mutation('active or uncertain',
            lambda: self.send_rereview(request_id='other-review'))
        self.fake.lose_ack = False
        self.finish_rereview()
        self.assertTrue(self.service.ao_room_status(self.room)['agreement']['agreed'])
        # A same-id resend returns the retained summary, never another POST.
        again = self.send_rereview()
        self.assertEqual(again['state'], 'completed')
        self.assertEqual(len(self.fake.posts), posts)

    def test_restart_preserves_validate_status_and_agreement(self):
        self.granted()
        self.send_rereview()
        self.finish_rereview()
        restarted = ao.Service(self.home, lambda url: self.fake)
        status = restarted.ao_room_status(self.room)
        self.assertTrue(status['agreement']['agreed'])
        self.assertEqual(status['spec_rereview']['status'], 'consumed')
        record, evidence = rereview.validate(restarted, self.state())
        self.assertEqual(record['inputs']['request_id'], self.REREVIEW_ID)

    def test_concurrent_identical_sends_post_once_and_consume_once(self):
        """Two sends of the same intent released on one barrier: exactly one POST, one consumption."""
        self.granted()
        barrier = threading.Barrier(2)
        results, errors = [], []
        def attempt():
            barrier.wait()
            try:
                results.append(self.service.ao_room_send(self.room, 'engineer', self.MESSAGE,
                                                         self.REREVIEW_ID, purpose='spec_review'))
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertFalse(errors)
        self.assertEqual([result['state'] for result in results], ['submitted', 'submitted'])
        self.assertEqual(len(self.fake.posts), 6)
        self.assertEqual(len(rereview._consumptions(self.directory())), 1)
        self.note_native_turn(self.REREVIEW_ID)

    def test_concurrent_different_id_send_never_posts_a_second_turn(self):
        """One authorized intent raced with a different request id: the wrong one refuses, one POST."""
        self.granted()
        barrier = threading.Barrier(2)
        results, errors = [], []
        def attempt(request_id):
            barrier.wait()
            try:
                results.append(self.service.ao_room_send(self.room, 'engineer', self.MESSAGE,
                                                         request_id, purpose='spec_review'))
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=attempt, args=(self.REREVIEW_ID,)),
                   threading.Thread(target=attempt, args=('concurrent-other',))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertRegex(str(errors[0]), 'active or uncertain|exact named re-review intent')
        self.assertEqual(len(self.fake.posts), 6)
        self.assertEqual(len(rereview._consumptions(self.directory())), 1)
        self.assertNotIn('concurrent-other', self.state()['requests'])
        self.assertIn(self.REREVIEW_ID, self.state()['requests'])
        self.note_native_turn(self.REREVIEW_ID)

    def test_repeated_same_id_send_never_posts_again(self):
        self.granted()
        self.send_rereview()
        posts = len(self.fake.posts)
        same = self.send_rereview()
        self.assertEqual(same['state'], 'submitted')
        self.assertEqual(len(self.fake.posts), posts)
        self.assert_refuses_without_mutation('active or uncertain',
            lambda: self.send_rereview(request_id='concurrent-other'))
        self.assertEqual(len(rereview._consumptions(self.directory())), 1)

    def test_consumption_survives_a_projection_crash_without_replay(self):
        """A crash after the durable consumption but before the state projection: the record
        stays, nothing POSTs, and every later operation reports the diagnosed hold."""
        self.granted()
        original_save = self.service.save
        def crashing(directory, state):
            if self.REREVIEW_ID in state['requests']:
                raise OSError('synthetic projection interruption after durable consumption')
            return original_save(directory, state)
        with patch.object(self.service, 'save', side_effect=crashing):
            with self.assertRaises(OSError):
                self.service.ao_room_send(self.room, 'engineer', self.MESSAGE, self.REREVIEW_ID,
                                          purpose='spec_review')
        self.assertNotIn(self.REREVIEW_ID, self.state()['requests'])
        self.assertEqual(len(rereview._consumptions(self.directory())), 1)
        self.assertEqual(len(self.fake.posts), 5)
        restarted = ao.Service(self.home, lambda url: self.fake)
        with self.assertRaisesRegex(ao.RoomError, 'request projection is missing'):
            rereview.validate(restarted, self.state())
        status = restarted.ao_room_status(self.room)
        self.assertEqual(status['spec_rereview']['status'], 'damaged')
        self.assertIn('projection is missing', status['spec_rereview']['error'])
        posts = len(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, 'projection is missing'):
            self.service.ao_room_send(self.room, 'engineer', self.MESSAGE, self.REREVIEW_ID,
                                      purpose='spec_review')
        self.assertEqual(len(self.fake.posts), posts)


class ReReviewQuotaHoldTests(ReReviewFixture):
    """Quota failure exhausts the grant; a semantic hold stays independent of transport."""

    def test_provider_limit_failure_settles_and_the_grant_stays_consumed(self):
        self.granted()
        self.send_rereview()
        self.fake.finish('engineer', 'Provider limit reached.')
        self.fake.snapshots['engineer']['turns'][-1].update(
            stopReason='max_tokens', error={'type': 'rate_limit', 'httpStatus': 429})
        self.service.ao_room_sync(self.room)
        audited = self.service.ao_room_outcome_audit(self.room)
        self.assertEqual(audited['outcome']['kind'], 'quota_limit')
        self.service.ao_room_outcome_resume(self.room, self.REREVIEW_ID, audited['outcome_sha256'],
            'post-quota', 'The correlated quota stop was diagnosed.',
            'Actual approval for only the named continuation.')
        self.assertTrue(self.state()['requests'][self.REREVIEW_ID].get('outcome_resume'))
        self.assertEqual(len(rereview._consumptions(self.directory())), 1)
        self.assertEqual(self.service.ao_room_status(self.room)['spec_rereview']['status'], 'consumed')
        self.assertEqual(len(self.fake.posts), 6)
        self.assert_refuses_without_mutation(REREVIEW_EXHAUSTED,
            lambda: self.service.ao_room_send(self.room, 'engineer', self.MESSAGE, 'post-quota',
                                              purpose='spec_review'))

    def test_semantic_hold_on_the_completed_rereview_blocks_until_independently_resolved(self):
        self.granted()
        self.send_rereview()
        self.finish_rereview()
        self.assertTrue(self.service.ao_room_status(self.room)['agreement']['agreed'])
        snapshot = self.fake.snapshots['engineer']
        snapshot['sessionFailures'] = [{'type': 'unclassified'}]
        held = self.service.ao_room_outcome_audit(self.room)
        self.assertTrue(held['outcome']['hold'])
        self.assertFalse(self.service.ao_room_status(self.room)['agreement']['agreed'])
        self.assert_refuses_without_mutation('Native semantic hold|rejected|did not accept',
            lambda: self.service.ao_room_handoff(self.room, str(self.repo)))
        snapshot['sessionFailures'] = []
        resolved = self.service.ao_room_outcome_audit(self.room)
        self.assertFalse(resolved['outcome']['hold'])
        self.assertTrue(self.service.ao_room_status(self.room)['agreement']['agreed'])


class ReReviewRegistrationTests(ReReviewFixture):
    def test_tools_registered_dispatched_and_in_the_runtime_roster(self):
        self.assertIn('ao_room_spec_rereview_audit', ao.TOOL_SCHEMAS)
        self.assertIn('ao_room_spec_rereview_grant', ao.TOOL_SCHEMAS)
        for name in ('ao_room_spec_rereview_audit', 'ao_room_spec_rereview_grant'):
            description, schema = ao.TOOL_SCHEMAS[name]
            self.assertIn('model dispatch', description)
            self.assertEqual(set(schema['required']), set(schema['properties']))
            self.assertIn('room_id', schema['properties'])
        self.assertIn('ao_spec_rereview.py', runtime.RUNTIME_FILES)
        controller = project_room.Service(self.home)
        listed = project_room_mcp.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}, controller)
        names = {item['name'] for item in listed['result']['tools']}
        self.assertIn('ao_room_spec_rereview_audit', names)
        self.assertIn('ao_room_spec_rereview_grant', names)
        with patch.object(ao, 'Service', return_value=self.service):
            with self.assertRaisesRegex(ao.RoomError, 'Invalid arguments'):
                controller.call('ao_room_spec_rereview_grant', {'room_id': self.room})
            response = project_room_mcp.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                'params': {'name': 'ao_room_spec_rereview_audit', 'arguments': self.audit_inputs()}},
                controller)
            self.assertFalse(response['result']['isError'])
            self.assertTrue(response['result']['structuredContent']['eligible'])
            granted = self.rereview_grant(audit={'audit_sha256': response['result']['structuredContent']['audit_sha256']})
            self.assertEqual(granted['remaining_spec_reviews'], 1)

    def test_cli_dispatches_the_audit_with_exact_complete_inputs(self):
        controller = project_room.Service(self.home)
        with patch.object(ao, 'Service', return_value=self.service):
            args_path = self.root / 'rereview-audit-args.json'
            args_path.write_text(json.dumps(self.audit_inputs()))
            output = io.StringIO()
            with redirect_stdout(output), patch.object(project_room, 'Service', return_value=controller), \
                    patch.object(project_room.signal, 'signal'):
                code = project_room.main(['--home', str(self.home), 'call',
                                          'ao_room_spec_rereview_audit', '--args-file', str(args_path)])
            self.assertEqual(code, 0)
            self.assertTrue(json.loads(output.getvalue())['eligible'])


class ReReviewDelegateTests(ReReviewFixture):
    """Unsettled delegate evidence refuses audit and admission through the ledger gate.

    Uses the shared fixture's room (provider 'none'): the audit must refuse even here when the
    delegate-ledger guard itself reports unfinished business, and an intact path must still
    admit. The delegate-ledger negatives in test_ao_acceptance_extension_compat cover the
    deepseek-provider room; this exercises the same ``assert_settled`` gate in this lane."""

    def test_delegate_settled_gate_runs_inside_audit(self):
        import ao_delegates
        calls = []
        original = ao_delegates.assert_settled
        def observed(home, state, directory=None):
            calls.append(True)
            return original(home, state, directory)
        with patch.object(ao_delegates, 'assert_settled', side_effect=observed):
            audited = self.rereview_audit()
        self.assertTrue(calls)
        self.assertTrue(audited['eligible'])

    def test_unsettled_delegate_jobs_refuse_audit_and_admission(self):
        import ao_delegates
        def unfinished(home, state, directory=None):
            raise ao.RoomError('Active or unresolved delegate jobs stop this room')
        with patch.object(ao_delegates, 'assert_settled', side_effect=unfinished):
            self.assert_refuses_without_mutation('Active or unresolved delegate jobs',
                                                 lambda: self.rereview_audit())
        self.granted()
        with patch.object(ao_delegates, 'assert_settled', side_effect=unfinished):
            self.assert_refuses_without_mutation('Active or unresolved delegate jobs',
                                                 lambda: self.send_rereview())


class ReReviewSamePathAuditTests(ReReviewFixture):
    """A supported same-path explicit outcome audit moves only the source-evidence pointer.

    ``native_outcome_source`` stays byte-identical, so ``_source_record`` early-returns and the
    re-review grant is untouched; only ``native_outcome_source_evidence`` moves to the new record.
    """

    def audit_same_paths(self):
        before = copy.deepcopy(self.state().get('native_outcome_source_evidence'))
        result = self.service.ao_room_outcome_audit(
            self.room, ao_database_path=str(self.database), native_transcript_path=str(self.transcript))
        after = self.state().get('native_outcome_source_evidence')
        self.assertNotEqual(after, before)  # The supported audit really moved the pointer.
        self.assertFalse(result['outcome']['hold'])
        self.assertFalse(result['model_dispatch'])

    def test_same_path_source_audit_before_consumption_keeps_the_send_admitted(self):
        self.granted()
        self.audit_same_paths()
        posts = len(self.fake.posts)
        self.send_rereview()
        self.assertEqual(len(self.fake.posts), posts + 1)
        status = self.service.ao_room_status(self.room)
        self.assertEqual(status['spec_rereview']['status'], 'consumed')

    def test_same_path_source_audit_after_the_fifth_agreement_keeps_handoff(self):
        self.granted()
        self.send_rereview()
        self.finish_rereview()
        self.assertTrue(self.service.ao_room_status(self.room)['agreement']['agreed'])
        self.audit_same_paths()
        self.assertTrue(self.service.ao_room_status(self.room)['agreement']['agreed'])
        handoff = self.service.ao_room_handoff(self.room, str(self.repo))
        self.assertEqual(handoff['spec_revision'], 3)


class ReReviewSettledFailureTests(ReReviewFixture):
    """A real native quota failure of the re-review turn settles without renewing the grant."""

    def test_quota_failure_of_the_fifth_turn_settles_and_stays_exhausted(self):
        import ao_native_outcome as native_outcome
        from test_ao_preacceptance_settled_history import _quota_native
        self.granted()
        self.send_rereview()
        self.fake.finish('engineer', state='failed')
        self.service.ao_room_sync(self.room)
        self.assertEqual(self.state()['requests'][self.REREVIEW_ID]['state'], 'uncertain')
        receipts = {key: (self.directory() / self.state()['requests'][key]['receipt']).read_bytes()
                    for key in ('charter-1', 'charter-2', 'charter-3', 'implementation', 'fourth')}
        posts = len(self.fake.posts)
        with patch.object(native_outcome, 'inspect', return_value=_quota_native(self.REREVIEW_ID)):
            audited = self.service.ao_room_outcome_audit(self.room)
            self.assertEqual(audited['outcome']['kind'], 'quota_limit')
            self.assertTrue(audited['resume_eligible'])
            self.service.ao_room_outcome_resume(self.room, self.REREVIEW_ID, audited['outcome_sha256'],
                'post-quota-successor', 'The correlated native quota stop was diagnosed.',
                'Actual approval for only the named continuation.')
        self.assertEqual(self.state()['requests'][self.REREVIEW_ID]['state'], 'settled_failure')
        status = self.service.ao_room_status(self.room)
        self.assertEqual(status['spec_rereview']['status'], 'consumed')
        self.assertEqual(status['spec_rereview']['remaining_spec_reviews'], 0)
        with self.assertRaisesRegex(ao.RoomError, REREVIEW_EXHAUSTED):
            self.send_rereview(request_id='post-quota-successor')
        with self.assertRaisesRegex(ao.RoomError, 'hold|exhausted'):
            self.send_rereview(request_id='another-review')
        self.assertEqual(len(self.fake.posts), posts)
        self.assertEqual(len(rereview._consumptions(self.directory())), 1)
        for key, content in receipts.items():
            self.assertEqual((self.directory() / self.state()['requests'][key]['receipt']).read_bytes(),
                             content)


class ReReviewSourceRecordTests(ReReviewFixture):
    """A moved native source validates only through its saved verified source audit."""

    def moved_audit(self):
        """A real moved-path outcome audit after the grant: source and evidence pointer move
        legitimately to the verified record for the moved bytes."""
        moved_db = self.root / 'moved-ao.db'
        shutil.copyfile(self.database, moved_db)
        self.database.unlink()
        moved_folder = self.root / 'native-moved'
        moved_folder.mkdir()
        moved_path = moved_folder / self.transcript.name
        self.transcript.rename(moved_path)
        result = self.service.ao_room_outcome_audit(self.room, ao_database_path=str(moved_db),
                                                    native_transcript_path=str(moved_path))
        return self.state()['native_outcome_source_evidence'], result

    def repoint_pointer_record(self, pointer, mutate):
        """Rewrite the pointed-at verified audit under its new content digest and repoint state."""
        record = rereview._read(self.directory() / pointer['path'])
        mutate(record)
        sha256 = ao.digest(record)
        path = 'outcomes/' + pointer['request_id'] + '/' + sha256 + '.json'
        rereview._store_once(self.directory() / path, record)
        state = self.state()
        state['native_outcome_source_evidence'] = {'path': path, 'sha256': sha256,
                                                   'request_id': pointer['request_id']}
        ao.atomic(self.directory() / 'state.json', state)

    def test_moved_source_with_a_verified_audit_still_validates_and_sends(self):
        self.granted()
        _, result = self.moved_audit()
        self.assertFalse(result['outcome']['hold'])
        record, _ = rereview.validate(self.service, self.state())
        self.assertEqual(record['inputs']['request_id'], self.REREVIEW_ID)
        posts = len(self.fake.posts)
        self.send_rereview()
        self.assertEqual(len(self.fake.posts), posts + 1)

    def test_changed_source_without_a_verified_audit_refuses(self):
        self.granted()
        state = self.state()
        state['native_outcome_source'] = {**state['native_outcome_source'],
                                          'transcript': str(self.root / 'unverified.jsonl')}
        state['native_outcome_source_evidence'] = 'not-a-pointer'
        ao.atomic(self.directory() / 'state.json', state)
        posts = len(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, 'requires its saved verified source audit'):
            rereview.validate(self.service, self.state())
        with self.assertRaises(ao.RoomError):
            self.service.ao_room_status(self.room)
        self.assertEqual(len(self.fake.posts), posts)

    def test_verified_audit_with_a_changed_owner_proof_refuses(self):
        self.granted()
        pointer, _ = self.moved_audit()
        def tamper(record):
            record['native_source_verification']['native_owner'] = {
                **record['native_source_verification']['native_owner'],
                'provider_conversation_id': 'other-native-owner'}
        self.repoint_pointer_record(pointer, tamper)
        posts = len(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, 'same native owner'):
            rereview.validate(self.service, self.state())
        with self.assertRaises(ao.RoomError):
            self.service.ao_room_status(self.room)
        self.assertEqual(len(self.fake.posts), posts)

    def test_verified_audit_with_malformed_verification_evidence_refuses(self):
        self.granted()
        pointer, _ = self.moved_audit()
        self.repoint_pointer_record(pointer, lambda record: record.update(
            native_source_verification='tampered'))
        posts = len(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, 'malformed verification evidence'):
            rereview.validate(self.service, self.state())
        self.assertEqual(len(self.fake.posts), posts)


class ReReviewExecutableFixture(ExecutableRuntimeMixin, ReReviewFixture):
    """The same room, but the AO engineer executable can break and be repaired through its journal."""

    TEMPLATE_ATTRS = ReReviewFixture.TEMPLATE_ATTRS + ('original_executable',)


class ReReviewExecutableTests(ReReviewExecutableFixture):
    """A same-owner executable repair is supported before consumption and after the fifth agreement."""

    def repair(self, tag):
        import ao_executable_binding as executable
        self.fake.snapshots['engineer']['controller'] = 'stopped'
        target = self.root / ('rereview-' + tag)
        target.write_text('#!/bin/sh\nprintf "2.1.283 (Claude Code)\\n"\n')
        target.chmod(0o700)
        launch = self.root / ('rereview-' + tag + '-launch')
        if launch.exists() or launch.is_symlink():
            launch.unlink()
        launch.symlink_to(target)
        previous = getattr(self, '_last_executable', self.original_executable)
        previous.unlink()
        self._last_executable = target
        return executable.bind(self.service, self.room, tag, str(target), str(launch),
                               str(self.database), 'Actual authorization for retained executable repair',
                               'Original executable was removed')

    def test_repairs_before_consumption_and_after_agreement_keep_every_check_valid(self):
        self.granted()
        self.repair('repair-one')
        pointer_one = copy.deepcopy(self.state()['executable_binding'])
        self.service.quiet(self.state())
        status = self.service.ao_room_status(self.room)
        self.assertEqual(status['spec_rereview']['status'], 'granted')
        rereview.validate(self.service, self.state())
        self.fake.snapshots['engineer']['controller'] = 'ready'
        self.send_rereview()
        self.finish_rereview()
        self.assertTrue(self.service.ao_room_status(self.room)['agreement']['agreed'])
        self.repair('repair-two')
        self.service.quiet(self.state())
        rereview.validate(self.service, self.state())
        self.assertTrue(self.service.ao_room_status(self.room)['agreement']['agreed'])
        handoff = self.service.ao_room_handoff(self.room, str(self.repo))
        self.assertEqual(handoff['spec_revision'], 3)
        # A pointer rolled back behind the newest committed repair refuses through the chain.
        state = self.state()
        state['executable_binding'] = pointer_one
        ao.atomic(self.directory() / 'state.json', state)
        with self.assertRaisesRegex(ao.RoomError, 'Unclaimed or missing executable binding intent'):
            rereview.validate(self.service, self.state())

    def test_orphan_repair_intent_and_altered_journal_record_refuse(self):
        import ao_executable_binding as executable
        self.granted()
        self.fake.snapshots['engineer']['controller'] = 'stopped'
        target = self.root / 'rereview-orphan-target'
        target.write_text('#!/bin/sh\nprintf "2.1.283 (Claude Code)\\n"\n')
        target.chmod(0o700)
        launch = self.root / 'rereview-orphan-launch'
        launch.symlink_to(target)
        self.original_executable.unlink()
        with patch.object(self.service, 'save',
                          side_effect=OSError('synthetic repair projection interruption')):
            with self.assertRaises(OSError):
                executable.bind(self.service, self.room, 'rereview-orphan', str(target), str(launch),
                                str(self.database), 'Actual repair approval preserves the retained owner.',
                                'Original executable is missing')
        # The durable unclaimed intent refuses validation until its exact call reconciles.
        with self.assertRaisesRegex(ao.RoomError, 'Unclaimed or missing executable binding intent'):
            rereview.validate(self.service, self.state())
        with self.assertRaises(ao.RoomError):
            self.service.ao_room_status(self.room)
        executable.bind(self.service, self.room, 'rereview-orphan', str(target), str(launch),
                        str(self.database), 'Actual repair approval preserves the retained owner.',
                        'Original executable is missing')
        rereview.validate(self.service, self.state())
        path = self.directory() / self.state()['executable_binding']['path']
        record = rereview._read(path)
        record['recorded_at'] = record.get('recorded_at', 0) + 1
        ao.atomic(path, record)
        with self.assertRaisesRegex(ao.RoomError, 'Immutable executable binding evidence changed'):
            rereview.validate(self.service, self.state())

    def test_changed_owner_repair_refuses_before_any_intent(self):
        import ao_executable_binding as executable
        self.granted()
        self.fake.snapshots['engineer']['controller'] = 'stopped'
        target = self.root / 'rereview-owner-target'
        target.write_text('#!/bin/sh\nprintf "2.1.283 (Claude Code)\\n"\n')
        target.chmod(0o700)
        launch = self.root / 'rereview-owner-launch'
        launch.symlink_to(target)
        self.original_executable.unlink()
        with sqlite3.connect(self.database) as db:
            db.execute("UPDATE sessions SET provider_conversation_id='changed-native-owner'")
            db.execute("UPDATE conversation_branches SET provider_conversation_id='changed-native-owner'")
        before = {str(p.relative_to(self.home)): p.read_bytes()
                  for p in self.home.rglob('*') if p.is_file()}
        posts = len(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, 'same native owner'):
            executable.bind(self.service, self.room, 'rereview-owner', str(target), str(launch),
                            str(self.database), 'Actual repair approval preserves the original owner.',
                            'Original executable is missing')
        self.assertEqual({str(p.relative_to(self.home)): p.read_bytes()
                          for p in self.home.rglob('*') if p.is_file()}, before)
        self.assertEqual(len(self.fake.posts), posts)

    def test_pending_receipt_reconciles_across_a_committed_repair(self):
        """An uncommitted re-review receipt plus a committed same-owner repair still reconciles
        only the identical grant call."""
        audited = self.rereview_audit()
        inputs = {'room_id': self.room, 'audit_sha256': audited['audit_sha256'],
                  'authorization': 'Actual authorization answer text.',
                  'authorization_reference': 'Recorded provenance.', 'diagnosis': 'Diagnosed correction.'}
        before = (self.directory() / 'state.json').read_bytes()
        with patch.object(self.service, 'save',
                          side_effect=OSError('synthetic grant projection interruption')):
            with self.assertRaises(OSError):
                self.service.ao_room_spec_rereview_grant(**inputs)
        self.assertEqual((self.directory() / 'state.json').read_bytes(), before)
        self.assertEqual(len(rereview._pending(self.directory())), 1)
        self.repair('pending-repair')
        reconciled = self.service.ao_room_spec_rereview_grant(**inputs)
        self.assertEqual(reconciled['remaining_spec_reviews'], 1)
        self.assertEqual(self.state()['spec_rereview']['request_id'], self.REREVIEW_ID)
        self.fake.snapshots['engineer']['controller'] = 'ready'
        posts = len(self.fake.posts)
        self.send_rereview()
        self.assertEqual(len(self.fake.posts), posts + 1)


class ReReviewRoutingFixture(ReReviewFixture):
    """The same room with a pinned historical guard, so a refresh has real guard bytes to change.

    Mirrors ``PreAcceptanceLiveRoutingRefreshTests`` in
    ``test_ao_preacceptance_review_extension``: the preparation pins a synthetic historical guard,
    so the refresh passes the 'already matches' early check and reaches the extension guard itself.
    """

    def configure_runtime(self):
        cli = self.root / 'fake-refresh-claude'
        cli.write_text('#!' + sys.executable + '\nprint("2.1.282 (Claude Code)")\n')
        cli.chmod(0o700)
        ao.atomic(self.home / 'config.json', {'claude_bin': str(cli),
                                              'claude_config_dir': str(self.claude_env)})

    def bind(self):
        import ao_routing_guard
        from test_ao_routing_refresh import OLD_GUARD
        original = Path.read_bytes
        source = Path(ao_routing_guard.__file__)
        with patch.object(Path, 'read_bytes',
                          lambda path: OLD_GUARD if path == source else original(path)):
            return super().bind()


class ReReviewRoutingRefreshTests(ReReviewRoutingFixture):
    """A routing refresh after the re-review grant is refused by the existing extension guard
    before any routing-refresh journal write; the room is never stranded."""

    def attempt_refresh(self):
        import ao_routing_refresh as refresh
        self.fake.snapshots['engineer']['controller'] = 'stopped'
        posts = len(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, 'agreement'):
            refresh.refresh(self.service, self.room, 'post-grant-refresh', str(self.database),
                            self.NATIVE, 'User authorizes the retained routing correction.',
                            'Retained routing predates the re-review.')
        self.assertFalse((self.directory() / refresh.BASE).exists())
        self.assertIsNone(self.state().get('routing_refresh'))
        self.assertEqual(len(self.fake.posts), posts)

    def test_refresh_refuses_while_the_grant_is_unconsumed(self):
        self.granted()
        self.attempt_refresh()
        # The lane is unharmed: the named intent still sends exactly once.
        posts = len(self.fake.posts)
        self.send_rereview()
        self.assertEqual(len(self.fake.posts), posts + 1)

    def test_refresh_refuses_after_the_fifth_agreement(self):
        self.granted()
        self.send_rereview()
        self.finish_rereview()
        self.assertTrue(self.service.ao_room_status(self.room)['agreement']['agreed'])
        self.attempt_refresh()
        self.assertTrue(self.service.ao_room_status(self.room)['agreement']['agreed'])
        self.service.ao_room_handoff(self.room, str(self.repo))


class ExtensionAtCeilingRereviewTests(ReviewExtensionFixture):
    """The extension granted at four dispatches: one accepting charter review and three
    non-accepting reviews exhaust the debate budget at dispatch 4, so the extension's own
    consumed review is the fifth dispatch. The re-review audit then refuses above the
    dispatch ceiling -- its single intent could never send -- instead of minting a lane."""

    REREVIEW_ID = 'spec-rereview'
    MESSAGE = 'Review the exact corrected specification revision 4.'

    def setUp(self):
        super().setUp()  # charter-1 accept, charter-2..4 changes_required
        summary = budget.summary(self.directory(), self.state())
        self.assertEqual((summary['debate_attempts'], summary['dispatches']), (3, 4))
        self.spec3 = self.register()
        self.audited = self.audit()
        self.extension_grant = self.grant()
        self.agree(decision='changes_required', revision=3, key='fifth')
        self.spec4 = self.register_corrected()

    def write_script(self):
        self.script = self.repo / 'scope_gate.py'
        self.script.write_text("from pathlib import Path\nassert Path('feature.txt').is_file()\n")
        self.script_sha256 = ao.digest(self.script.read_bytes())
        return self.script

    def corrected_gates(self):
        return [[sys.executable, '-B', str(self.script), self.script_sha256]] + [list(g) for g in self.gates]

    def register_corrected(self):
        self.write_script()
        return self.service.ao_room_spec_put(self.room, 4,
            'Review the corrected validation charter after the refused extension review.',
            self.corrected_gates(),
            'Astra approves this exact corrected charter under the actual product decision')

    def audit_inputs(self, **changes):
        source = self.state()['requests']['fifth']
        raw = ao_workflow.raw_final_text(self.directory(), source)
        inputs = {'room_id': self.room, 'spec_revision': 4, 'spec_sha256': self.spec4['sha256'],
                  'source_request_id': 'fifth',
                  'source_receipt_sha256': source['receipt_sha256'],
                  'source_final_text_sha256': ao.digest(raw.encode()),
                  'source_classification': 'changes_required',
                  'request_id': self.REREVIEW_ID,
                  'message_sha256': ao.digest(self.MESSAGE.encode()),
                  'gate_artifacts': [str(self.script)],
                  'native_session_id': self.NATIVE, 'native_owner_database': str(self.database)}
        inputs.update(changes)
        return inputs

    def room_files(self):
        return {str(p.relative_to(self.directory())): p.read_bytes()
                for p in self.directory().rglob('*') if p.is_file()}

    def test_rereview_audit_refuses_above_the_dispatch_ceiling(self):
        # The consumed extension review was itself a non-accepting completed dispatch.
        summary = budget.summary(self.directory(), self.state())
        self.assertEqual((summary['debate_attempts'], summary['dispatches']), (4, 5))
        posts = len(self.fake.posts)
        before = self.room_files()
        with self.assertRaisesRegex(ao.RoomError, 'five-dispatch ceiling'):
            self.service.ao_room_spec_rereview_audit(**self.audit_inputs())
        self.assertEqual(len(self.fake.posts), posts)
        self.assertEqual(self.room_files(), before)
        with self.assertRaisesRegex(ao.RoomError, 'Five Fable spec-review dispatches reached the room ceiling'):
            self.send('spec_review', 'sixth')
        self.assertEqual(len(self.fake.posts), posts)
        status = self.service.ao_room_status(self.room)
        self.assertEqual(status['spec_review_budget']['dispatches'], 5)
        self.assertFalse(status['spec_review_budget']['ordinary_review_capacity'])
        import ao_review_extension
        self.assertIsNotNone(ao_review_extension.validate(self.service, self.state()))


class ExtensionRecordTamperTests(ReReviewFixture):
    """The lane resolves its position through the committed extension's own validator, so a
    tampered granted maximum surfaces as the extension's refusal, never silently renumbered."""

    def test_modified_extension_maximum_refuses_through_the_extension_validator(self):
        self.granted()
        self.send_rereview()
        self.finish_rereview()
        reference = self.state()['spec_review_extension']
        path = self.directory() / reference['receipt']
        record = ao.read(path)
        record['maximum_spec_review_attempts'] += 1
        ao.atomic(path, record)
        with self.assertRaisesRegex(ao.RoomError, 'Committed review-extension grant was modified'):
            rereview.validate(self.service, self.state())


if __name__ == '__main__':
    unittest.main()
