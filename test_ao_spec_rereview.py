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
import sqlite3
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import ao_project_room as ao
import ao_spec_rereview as rereview
import ao_workflow
import project_room
import project_room_mcp
import project_room_runtime as runtime
from test_ao_preacceptance_review_extension import PreAcceptanceFixture

EXHAUSTED = 'fourth Fable review intent is exhausted'
REREVIEW_EXHAUSTED = 'one re-review intent is exhausted'


class ReReviewFixture(PreAcceptanceFixture):
    """The consumed charter extension's fourth review completed with the noncanonical negative
    verdict; the operator then registered the corrected revision-3 charter whose first gate
    declares a synthetic artifact path plus its bytes hash, exactly as an audited gate does."""

    MESSAGE = 'Review the exact corrected specification revision 3.'
    REREVIEW_ID = 'spec-rereview'

    def setUp(self):
        super().setUp()
        self.through_fourth()
        self.spec3 = self.register_corrected()

    def through_fourth(self):
        self.propose_scope_change()
        self.spec2 = self.register()
        self.extension_grant = self.grant()
        self.send('spec_review', 'fourth')
        self.fake.finish('engineer', json.dumps(self.source_verdict()))
        self.service.ao_room_sync(self.room)

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
        self.propose_scope_change()
        self.spec2 = self.register()
        self.extension_grant = self.grant()

    def register_corrected(self):
        self.write_script()
        return {'revision': 3, 'sha256': '0' * 64}


class ReReviewUnconsumedTests(ReReviewUnconsumedFixture):
    def test_extension_not_consumed_refuses_the_audit(self):
        self.assert_refuses_without_mutation('consumed by exactly one completed fourth review',
            lambda: self.rereview_audit(source_request_id='implementation',
                                        source_receipt_sha256='0' * 64,
                                        source_final_text_sha256='0' * 64))


class ReReviewProseSourceFixture(ReReviewFixture):
    """The fourth review finished as plain prose, never one JSON object."""

    def through_fourth(self):
        self.propose_scope_change()
        self.spec2 = self.register()
        self.extension_grant = self.grant()
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

    def test_post_failure_after_consumption_stays_durable_and_never_replays(self):
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

    def test_concurrent_or_repeated_sends_post_once_and_consume_once(self):
        self.granted()
        self.send_rereview()
        posts = len(self.fake.posts)
        same = self.send_rereview()
        self.assertEqual(same['state'], 'submitted')
        self.assertEqual(len(self.fake.posts), posts)
        self.assert_refuses_without_mutation('active or uncertain',
            lambda: self.send_rereview(request_id='concurrent-other'))
        self.assertEqual(len(rereview._consumptions(self.directory())), 1)


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


if __name__ == '__main__':
    unittest.main()
