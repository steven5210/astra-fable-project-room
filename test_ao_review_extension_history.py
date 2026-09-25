"""Retained pre-grant reconciliation ancestry; only synthetic real-Service fixtures."""
from datetime import datetime, timezone
import copy
import hashlib
import inspect
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import ao_project_room as ao
import ao_history_reconciliation as history
import ao_review_extension as extension
from test_ao_review_extension import ReviewExtensionFixture


class RetainedHistoryTests(unittest.TestCase):
    def setUp(self):
        f = self.f = ReviewExtensionFixture('runTest')
        self.addCleanup(f.doCleanups); f.setUp()
        self.request_id = 'pregrant-quota'
        f.send('correction', self.request_id)
        f.fake.finish('engineer', '')
        snapshot = f.fake.snapshots['engineer']; snapshot['history_truncated'] = True
        with patch.object(history, 'requires_strict_history', return_value=False):
            f.service.ao_room_sync(f.room)
        snapshot['history_truncated'] = False
        request = self.current()
        self.receipt_path = f.directory() / request['receipt']
        self.receipt_bytes = self.receipt_path.read_bytes()
        self.assertIs(json.loads(self.receipt_bytes)['history_truncated'], True)
        self.transcript = f.root / 'synthetic-native-owner.jsonl'
        stamp = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat()
        self.events = [
            {'type': 'user', 'uuid': 'pregrant-caller', 'sessionId': 'synthetic-native-owner',
             'timestamp': stamp(request['created_at'] + .1), 'origin': {'kind': 'human'},
             'message': {'content': request['text']}},
            {'type': 'assistant', 'uuid': 'pregrant-quota', 'sessionId': 'synthetic-native-owner',
             'timestamp': stamp(request['created_at'] + .2), 'isApiErrorMessage': True,
             'error': 'rate_limit', 'apiErrorStatus': 429, 'message': {'model': '<synthetic>'}},
        ]
        self.write_native()
        self.assertTrue(self.audit()['resume_eligible'])

    def current(self):
        return self.f.state()['requests'][self.request_id]

    def files(self):
        return {str(p.relative_to(self.f.directory())): p.read_bytes()
                for p in self.f.directory().rglob('*') if p.is_file()}

    def write_native(self):
        self.transcript.write_text(''.join(json.dumps(e) + '\n' for e in self.events))

    def audit(self):
        return self.f.service.ao_room_outcome_audit(self.f.room, ao_database_path=str(self.f.database),
                                                   native_transcript_path=str(self.transcript))

    def refuse(self):
        with self.assertRaisesRegex(ao.RoomError, 'Synthetic retained-proof refusal'):
            with self.f.service.locked(self.f.room):
                raise ao.RoomError('Synthetic retained-proof refusal')

    def grant(self, prior_head=False, stale=False):
        if prior_head or stale:
            self.refuse()
            if not stale:
                self.assertTrue(self.audit()['resume_eligible'])
        result = self.f.grant()
        self.original = self.current()
        self.original_files = self.files()
        self.posts = len(self.f.fake.posts)
        self.assertEqual(result['remaining_spec_reviews'], 1)
        return result

    def status(self, available=True):
        before = self.files()
        result = self.f.service.ao_room_status(self.f.room)
        self.assertEqual(self.files(), before)
        self.assertEqual(result['spec_review_extension']['remaining_spec_reviews'], 1)
        quality = result['engineer_context'].get('quality_first_review')
        if quality is not None:
            self.assertEqual(quality['delivery'], 'verified_delivered' if available else 'unavailable_integrity')
        return result

    def preserved(self):
        self.assertEqual(self.receipt_path.read_bytes(), self.receipt_bytes)
        now = self.files()
        self.assertEqual({p: now[p] for p in self.original_files if p != 'state.json'},
                         {p: raw for p, raw in self.original_files.items() if p != 'state.json'})
        self.assertEqual(len(self.f.fake.posts), self.posts)

    def renewal(self):
        previous = self.current()[history.PROOF]
        self.refuse()
        head = self.current()[history.INVALIDATION]
        self.status(available=False)
        self.refuse()
        self.assertEqual(self.current()[history.INVALIDATION], head)
        self.assertTrue(self.audit()['resume_eligible'])
        self.assertNotEqual(self.current()[history.PROOF], previous)
        self.status()
        self.preserved()

    def test_pinned_proof_only_renews_and_retains_original_files(self):
        self.grant()
        self.assertNotIn(history.INVALIDATION, self.original)
        self.renewal()

    def test_pinned_proof_and_head_allow_multiple_renewals_after_restart(self):
        self.grant(prior_head=True)
        self.assertIn(history.INVALIDATION, self.original)
        self.renewal()
        self.f.service = ao.Service(self.f.home, lambda url: self.f.fake)
        self.renewal()

    def test_stale_proof_can_be_pinned_then_explicitly_renewed(self):
        self.grant(stale=True)
        self.status(available=False)
        self.assertTrue(self.audit()['resume_eligible'])
        self.status()
        self.preserved()

    def test_unknown_observation_between_invalidation_and_renewal_is_retained(self):
        self.grant()
        self.refuse()
        invalidation = ao.read(self.f.directory() / 'history-reconciliation-invalidations' /
                               self.request_id / (self.current()[history.INVALIDATION] + '.json'))
        original_error = self.events[-1]['error']
        self.events[-1]['error'] = 'invalid_request'; self.events[-1]['apiErrorStatus'] = 400
        self.write_native()
        self.f.service.ao_room_sync(self.f.room)
        intermediate = self.current()['semantic_outcome_sha256']
        self.assertNotEqual(intermediate, invalidation['outcome_sha256'])
        unknown = ao.read(self.f.directory() / self.current()['semantic_outcome'])
        self.assertNotIn(history.PROOF, unknown)
        self.events[-1]['error'] = original_error; self.events[-1]['apiErrorStatus'] = 429
        self.write_native()
        self.assertTrue(self.audit()['resume_eligible'])
        proof = history.load_proof(self.f.directory(), self.current())
        self.assertEqual(proof['supersedes_outcome_sha256'], intermediate)
        self.status(); self.preserved()

    def test_historical_source_path_stays_anchored_after_explicit_move(self):
        self.grant()
        old = self.transcript
        self.transcript = self.f.root / 'moved' / old.name
        self.transcript.parent.mkdir(); self.transcript.write_bytes(old.read_bytes())
        self.assertTrue(self.audit()['resume_eligible'])
        old.unlink()
        self.status(); self.preserved()

    def test_new_grant_manifest_includes_original_history_records(self):
        self.grant(prior_head=True)
        _, evidence = extension.validate(self.f.service, self.f.state())
        for area, key in (('history-reconciliations', history.PROOF),
                          ('history-reconciliation-invalidations', history.INVALIDATION)):
            relative = area + '/' + self.request_id + '/' + self.original[key] + '.json'
            self.assertIn(relative, evidence['manifest'])
        self.preserved()

    def test_legacy_manifest_keeps_two_arguments_and_its_original_content(self):
        self.grant(prior_head=True)
        with patch.object(extension, '_HistoryEvidence', side_effect=AssertionError('Legacy manifest gained history authority')):
            legacy = extension._manifest(self.f.directory(), self.f.state())
        self.assertFalse(any(path.startswith('history-reconciliation') for path in legacy))
        _, evidence = extension.validate(self.f.service, self.f.state())
        self.assertTrue(any(path.startswith('history-reconciliation') for path in evidence['manifest']))
        self.preserved()

    def test_audit_keeps_hold_and_one_named_release_allows_exactly_one_fourth_review(self):
        self.grant()
        self.renewal()
        with self.assertRaisesRegex(ao.RoomError, 'Native semantic hold'):
            self.f.send('spec_review', 'fourth-charter')
        self.assertNotIn('fourth-charter', self.f.state()['requests'])
        audited = self.audit()
        self.f.service.ao_room_outcome_resume(self.f.room, self.request_id, audited['outcome_sha256'],
            'fourth-charter', 'Inspected exact retained failure', 'Synthetic user authorizes one continuation')
        self.f.send('spec_review', 'fourth-charter')
        self.f.send('spec_review', 'fourth-charter')
        self.assertEqual(len(self.f.fake.posts), self.posts + 1)
        self.assertEqual(self.f.service.ao_room_status(self.f.room)['spec_review_extension']['remaining_spec_reviews'], 0)
        self.assertEqual(self.receipt_path.read_bytes(), self.receipt_bytes)

    def test_legacy_grant_manifest_needs_no_rewrite_to_retain_renewal(self):
        original_manifest = extension._grant_manifest
        def old_manifest(*args):
            return {path: digest for path, digest in original_manifest(*args).items()
                    if not path.startswith(('history-reconciliations/', 'history-reconciliation-invalidations/'))}
        with patch.object(extension, '_grant_manifest', side_effect=old_manifest):
            self.grant(prior_head=True)
        self.renewal()
        _, evidence = extension.validate(self.f.service, self.f.state())
        self.assertFalse(any(path.startswith('history-reconciliation') for path in evidence['manifest']))
        self.preserved()

    def assert_retention_refuses_readonly(self):
        before = self.files(); posts = len(self.f.fake.posts)
        with self.assertRaises(ao.RoomError):
            extension.validate(self.f.service, self.f.state())
        self.assertEqual(before, self.files())
        self.assertEqual(len(self.f.fake.posts), posts)

    def persist(self, row, area):
        sha = ao.digest(row)
        ao.atomic(self.f.directory() / area / self.request_id / (sha + '.json'), row)
        return sha

    def test_removing_original_pointers_or_records_refuses_after_valid_renewal(self):
        self.grant(prior_head=True); self.renewal()
        saved = self.f.state()
        for key, area in ((history.PROOF, 'history-reconciliations'),
                          (history.INVALIDATION, 'history-reconciliation-invalidations')):
            for value in (None, 'remove'):
                with self.subTest(key=key, value=value):
                    state = copy.deepcopy(saved)
                    if value == 'remove': state['requests'][self.request_id].pop(key)
                    else: state['requests'][self.request_id][key] = value
                    ao.atomic(self.f.directory() / 'state.json', state)
                    self.assert_retention_refuses_readonly()
            ao.atomic(self.f.directory() / 'state.json', saved)
            path = self.f.directory() / area / self.request_id / (self.original[key] + '.json')
            raw = path.read_bytes()
            for mode in ('missing', 'modified'):
                with self.subTest(key=key, mode=mode):
                    if mode == 'missing': path.unlink()
                    else: path.write_bytes(b'{}\n')
                    self.assert_retention_refuses_readonly()
                    path.write_bytes(raw)
        extension.validate(self.f.service, self.f.state())

    def test_rehashed_contradictory_proof_anchors_refuse(self):
        self.grant(); self.renewal()
        saved = self.f.state()
        proof = history.load_proof(self.f.directory(), self.current())
        probes = [('room_id',), ('request_id',), ('receipt_sha256',), ('text_sha256',),
                  ('inputs', 'session_id'), ('inputs', 'provider_turn_id'), ('inputs', 'messages_sha256'),
                  ('inputs', 'native', 'source', 'session_id'),
                  ('inputs', 'native', 'source', 'native_session_id')]
        for keys in probes:
            with self.subTest(keys=keys):
                row = copy.deepcopy(proof); target = row
                for key in keys[:-1]: target = target[key]
                target[keys[-1]] = '0' * 64 if keys[-1].endswith('sha256') else 'foreign-owner'
                sha = self.persist(row, 'history-reconciliations')
                state = copy.deepcopy(saved); state['requests'][self.request_id][history.PROOF] = sha
                ao.atomic(self.f.directory() / 'state.json', state)
                self.assert_retention_refuses_readonly()
        ao.atomic(self.f.directory() / 'state.json', saved)

    def test_non_descendant_proof_and_wrong_stale_head_refuse(self):
        self.grant(prior_head=True); self.renewal()
        saved = self.f.state()
        proof = history.load_proof(self.f.directory(), self.current())
        branch = {**proof, 'invalidation_sha256': None, 'supersedes_outcome_sha256': None}
        state = copy.deepcopy(saved)
        state['requests'][self.request_id][history.PROOF] = self.persist(branch, 'history-reconciliations')
        state['requests'][self.request_id].pop(history.INVALIDATION)
        ao.atomic(self.f.directory() / 'state.json', state)
        self.assert_retention_refuses_readonly()
        ao.atomic(self.f.directory() / 'state.json', saved)
        self.refuse()
        state = self.f.state()
        head = ao.read(self.f.directory() / 'history-reconciliation-invalidations' / self.request_id /
                       (self.current()[history.INVALIDATION] + '.json'))
        head['previous_sha256'] = None
        state['requests'][self.request_id][history.INVALIDATION] = self.persist(head, 'history-reconciliation-invalidations')
        ao.atomic(self.f.directory() / 'state.json', state)
        self.assert_retention_refuses_readonly()

    def test_original_head_cannot_be_replaced_by_a_rehashed_sibling(self):
        self.grant(prior_head=True)
        saved = self.f.state()
        request = self.current(); receipt = ao_workflow_receipt(self.f.directory(), request)
        head = ao.read(self.f.directory() / 'history-reconciliation-invalidations' / self.request_id /
                       (request[history.INVALIDATION] + '.json'))
        head['reason'] = 'A coherent unrelated branch'
        sibling = self.persist(head, 'history-reconciliation-invalidations')
        proof = history.load_proof(self.f.directory(), request)
        proof['invalidation_sha256'] = sibling
        current = copy.deepcopy(request)
        current[history.PROOF] = self.persist(proof, 'history-reconciliations')
        current[history.INVALIDATION] = sibling
        # The sibling is structurally valid alone but does not retain the original P/I anchors.
        with extension._HistoryEvidence(self.f.directory(), 'synthetic-native-owner') as evidence:
            evidence.lineage(current, receipt)
            with self.assertRaises(ao.RoomError): evidence.retained(request, current, receipt)
        self.assertEqual(self.f.state(), saved)

    def test_bound_applies_across_unique_records_and_cached_reads_are_not_recharged(self):
        self.grant(prior_head=True); self.renewal()
        receipt = ao_workflow_receipt(self.f.directory(), self.original)
        with extension._HistoryEvidence(self.f.directory(), 'synthetic-native-owner') as evidence:
            evidence.retained(self.original, self.current(), receipt)
            used = evidence.total
            evidence.retained(self.original, self.current(), receipt)
            self.assertEqual(evidence.total, used)
        with patch.object(extension, 'MAX_HISTORY_TOTAL_BYTES', used - 1):
            self.assert_retention_refuses_readonly()
        with patch.object(extension, 'MAX_HISTORY_INVALIDATIONS', 1):
            self.assert_retention_refuses_readonly()

    def test_new_outcome_reference_does_not_authorize_foreign_identity(self):
        self.grant(); self.renewal()
        saved = self.f.state(); proof = history.load_proof(self.f.directory(), self.current())
        old_outcome = ao.read(self.f.directory() / 'outcomes' / self.request_id /
                              (proof['supersedes_outcome_sha256'] + '.json'))
        old_outcome['text_sha256'] = '0' * 64
        proof['supersedes_outcome_sha256'] = self.persist(old_outcome, 'outcomes')
        state = copy.deepcopy(saved)
        state['requests'][self.request_id][history.PROOF] = self.persist(proof, 'history-reconciliations')
        ao.atomic(self.f.directory() / 'state.json', state)
        self.assert_retention_refuses_readonly()


def ao_workflow_receipt(directory, request):
    import ao_workflow
    return ao_workflow.completed_receipt(directory, request)


class RoutingManifestCompatibility(unittest.TestCase):
    def test_pending_refresh_keeps_legacy_manifest_and_reconciles_exact_intent(self):
        import ao_routing_refresh as refresh
        from test_ao_routing_refresh import RoutingRefreshTests
        f = RoutingRefreshTests('runTest')
        self.addCleanup(f.doCleanups); f.setUp()
        with patch.object(extension, '_HistoryEvidence', side_effect=AssertionError('Routing gained new grant-history behavior')):
            f.pending()
            path = f.directory() / refresh.BASE / 'refresh-one.json'
            original = path.read_bytes(); record = json.loads(original)
            self.assertEqual(record['evidence']['retained_manifest'], refresh._retained_manifest(f.directory(), f.state()))
            f.do_refresh()
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(f.fake.posts, f.posts)


class PendingLegacyGrantTests(unittest.TestCase):
    def setUp(self):
        self.history = RetainedHistoryTests('runTest')
        self.addCleanup(self.history.doCleanups)
        self.history.setUp()
        self.f = self.history.f

    def pending(self, prior_head=True, modern=False):
        if prior_head:
            self.history.refuse()
            self.assertTrue(self.history.audit()['resume_eligible'])
        # This independently pinned source is the exact pre-repair manifest
        # oracle from 2b02be7, not a projection of the new grant manifest.
        self.assertEqual(hashlib.sha256(inspect.getsource(extension._manifest).encode()).hexdigest(),
                         '1141cf43d93a988c937b78e772b55d2a6bb51dd1cf35823cde98999893bee566')
        manifest = extension._grant_manifest if modern else lambda directory, state, owner: extension._manifest(directory, state)
        with patch.object(extension, '_grant_manifest', side_effect=manifest):
            self.inputs = self.f.grant_inputs()
            before = (self.f.directory() / 'state.json').read_bytes()
            # Abrupt process loss must not simulate a handled refusal, whose
            # existing lock handler deliberately invalidates an established proof.
            with patch.object(self.f.service, 'save', side_effect=SystemExit('Synthetic crash before state commit')):
                with self.assertRaises(SystemExit):
                    self.f.service.ao_room_spec_review_extend(**self.inputs)
        self.assertEqual((self.f.directory() / 'state.json').read_bytes(), before)
        paths = extension._pending(self.f.directory())
        self.assertEqual(len(paths), 1)
        self.receipt_path = paths[0]
        self.receipt = ao.read(self.receipt_path)
        self.saved_audit = extension._audit(self.f.directory(), self.inputs['audit_sha256'])
        self.assertEqual(self.receipt['evidence_sha256'], ao.digest(self.saved_audit['evidence']))
        self.assertEqual(self.receipt['before_state_sha256'], ao.digest(self.f.state()))
        self.f.service = ao.Service(self.f.home, lambda url: self.f.fake)
        self.snapshot = self.history.files()
        self.original_inputs = copy.deepcopy(self.inputs)

    def restore(self):
        for path in self.f.directory().rglob('*'):
            if (path.is_file() or path.is_symlink()) and str(path.relative_to(self.f.directory())) not in self.snapshot:
                path.unlink()
        for name, raw in self.snapshot.items():
            path = self.f.directory() / name
            if path.is_symlink():
                path.unlink()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        self.inputs = copy.deepcopy(self.original_inputs)

    def rewrite_saved(self, change):
        saved = copy.deepcopy(self.saved_audit)
        change(saved['evidence'])
        sha = ao.digest(saved)
        ao.atomic(self.f.directory() / extension.BASE / 'audits' / (sha + '.json'), saved)
        self.inputs = {**self.original_inputs, 'audit_sha256': sha}
        receipt = copy.deepcopy(self.receipt)
        receipt['inputs'] = {k: v for k, v in self.inputs.items() if k != 'room_id'}
        receipt['before_state_sha256'] = saved['evidence']['state_sha256']
        receipt['evidence_sha256'] = ao.digest(saved['evidence'])
        ao.atomic(self.receipt_path, receipt)

    def refuses(self, pattern='.', inputs=None, changed_files=()):
        before = self.history.files()
        posts = len(self.f.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, pattern):
            self.f.service.ao_room_spec_review_extend(**(inputs or self.inputs))
        after = self.history.files()
        self.assertNotIn('spec_review_extension', self.f.state())
        self.assertEqual(set(self.f.state()['requests']), set(json.loads(before['state.json'])['requests']))
        for name, raw in before.items():
            if name != 'state.json' and name not in changed_files:
                self.assertEqual(after.get(name), raw, name)
        self.assertEqual(len(self.f.fake.posts), posts)

    def reconciles(self):
        before = self.history.files()
        posts = len(self.f.fake.posts)
        result = self.f.service.ao_room_spec_review_extend(**self.inputs)
        state = self.f.state()
        self.assertEqual({k: v for k, v in state.items() if k != 'spec_review_extension'},
                         json.loads(before['state.json']))
        self.assertEqual(result['remaining_spec_reviews'], 1)
        self.assertEqual(result['receipt_sha256'], ao.digest(self.receipt))
        self.assertEqual(result['audit_sha256'], self.inputs['audit_sha256'])
        self.assertIs(result['model_dispatch'], False)
        self.assertEqual(self.f.service.ao_room_spec_review_extend(**self.inputs), result)
        self.assertEqual(extension.validate(self.f.service, self.f.state())[1], self.saved_audit['evidence'])
        after = self.history.files()
        self.assertEqual(set(after), set(before))
        self.assertEqual({k: v for k, v in after.items() if k != 'state.json'},
                         {k: v for k, v in before.items() if k != 'state.json'})
        self.assertEqual(len(self.f.fake.posts), posts)

    def test_pre_repair_proof_only_pending_receipt_reconciles_after_restart(self):
        self.pending(prior_head=False)
        self.reconciles()

    def test_pre_repair_proof_and_head_pending_receipt_reconciles_after_restart(self):
        self.pending()
        self.reconciles()

    def test_modern_pending_receipt_still_requires_and_preserves_exact_evidence(self):
        self.pending(modern=True)
        self.reconciles()

    def test_old_audit_without_pending_receipt_has_no_compatibility_authority(self):
        self.pending()
        self.receipt_path.unlink()
        self.refuses('audit is stale')
        self.assertEqual(extension._pending(self.f.directory()), [])

    def test_different_pending_request_or_payload_refuses_before_inspection(self):
        self.pending()
        for key, value in (('request_id', 'another-extension'), ('authorization', 'Another authorization'),
                           ('diagnosis', 'Another diagnosis'), ('audit_sha256', '0' * 64)):
            with self.subTest(key=key):
                self.restore()
                with patch.object(extension, '_inspect', side_effect=AssertionError('Foreign payload reached inspection')):
                    self.refuses('another payload', inputs={**self.inputs, key: value})

    def test_other_saved_evidence_fields_cannot_be_projected_away(self):
        self.pending()
        changes = [lambda e: e['native']['engineer'].__setitem__('activities_sha256', '0' * 64),
                   lambda e: e['native_owner'].__setitem__('provider_conversation_id', 'foreign-native-owner'),
                   lambda e: e.__setitem__('state_sha256', '0' * 64),
                   lambda e: e.__setitem__('unexpected', 'untrusted')]
        for index, change in enumerate(changes):
            with self.subTest(change=index):
                self.restore()
                self.rewrite_saved(change)
                self.refuses('audit is stale')

    def test_numeric_type_change_in_saved_state_is_not_exact_legacy_evidence(self):
        self.pending()
        self.assertIs(type(self.saved_audit['evidence']['state']['version']), int)
        self.rewrite_saved(lambda e: e['state'].__setitem__('version', float(e['state']['version'])))
        self.refuses('audit is stale')

    def numeric_receipt_refuses(self, modern):
        self.pending(modern=modern)
        fields = ('version', 'additional_spec_reviews', 'maximum_spec_review_attempts')
        for field in fields:
            original = self.receipt[field]
            values = (float(original), True) if original == 1 else (float(original),)
            for value in values:
                with self.subTest(field=field, value_type=type(value).__name__):
                    self.restore()
                    receipt = ao.read(self.receipt_path)
                    receipt[field] = value
                    ao.atomic(self.receipt_path, receipt)
                    self.refuses('Pending review-extension receipt differs from the recomputed grant')

    def test_legacy_pending_receipt_numeric_types_must_match_recomputed_digest(self):
        self.numeric_receipt_refuses(modern=False)

    def test_modern_pending_receipt_numeric_types_must_match_recomputed_digest(self):
        self.numeric_receipt_refuses(modern=True)

    def test_original_manifest_entries_must_match_without_removal_or_addition(self):
        self.pending()
        name = next(iter(self.saved_audit['evidence']['manifest']))
        changes = [lambda e: e['manifest'].pop(name),
                   lambda e: e['manifest'].__setitem__(name, '0' * 64),
                   lambda e: e['manifest'].__setitem__('unrelated-evidence.json', '0' * 64)]
        for index, change in enumerate(changes):
            with self.subTest(change=index):
                self.restore()
                self.rewrite_saved(change)
                self.refuses('audit is stale')

    def test_partial_or_changed_modern_history_manifests_are_not_legacy(self):
        self.pending()
        current, _ = extension._inspect(self.f.service, self.f.directory(), self.f.state(),
                                        self.saved_audit['evidence']['target'], reconcile=True)
        additions = {k: v for k, v in current['manifest'].items()
                     if k.startswith(('history-reconciliations/', 'history-reconciliation-invalidations/'))}
        self.assertGreaterEqual(len(additions), 3)
        first = next(iter(additions))
        variants = [{key: value} for key, value in additions.items()]
        variants += [{**additions, first: '0' * 64}, {k: v for k, v in additions.items() if k != first}]
        for index, extra in enumerate(variants):
            with self.subTest(variant=index):
                self.restore()
                self.rewrite_saved(lambda e: e['manifest'].update(extra))
                self.refuses('audit is stale')

    def test_missing_modified_foreign_or_unsafe_history_cannot_reconcile_legacy(self):
        self.pending()
        request = self.history.current()
        for area, key in (('history-reconciliations', history.PROOF),
                          ('history-reconciliation-invalidations', history.INVALIDATION)):
            path = self.f.directory() / area / self.history.request_id / (request[key] + '.json')
            for mode in ('missing', 'modified', 'foreign', 'symlink'):
                with self.subTest(area=area, mode=mode):
                    self.restore()
                    if mode == 'missing':
                        path.unlink()
                    elif mode == 'modified':
                        path.write_bytes(b'{}\n')
                    elif mode == 'foreign':
                        row = ao.read(path); row['room_id'] = 'foreign-room'
                        ao.atomic(path, row)
                    else:
                        target = self.f.root / 'linked-history.json'
                        target.write_bytes(path.read_bytes()); path.unlink(); path.symlink_to(target)
                    self.refuses()

    def test_second_full_inspection_detects_new_history_and_original_manifest_changes(self):
        self.pending()
        request = self.history.current()
        history_path = 'history-reconciliations/' + self.history.request_id + '/' + request[history.PROOF] + '.json'
        original_path = request['semantic_outcome']
        for name in (history_path, original_path):
            with self.subTest(path=name):
                self.restore()
                inspect_now = extension._inspect
                calls = []
                def changing_inspection(*args, **kwargs):
                    result = inspect_now(*args, **kwargs)
                    calls.append(result[0])
                    if len(calls) == 1:
                        path = self.f.directory() / name
                        path.write_bytes(path.read_bytes() + b'\n')
                    return result
                with patch.object(extension, '_inspect', side_effect=changing_inspection):
                    self.refuses('evidence changed before commit', changed_files=(name,))
                self.assertEqual(len(calls), 2)
                self.assertNotEqual(calls[0]['manifest'][name], calls[1]['manifest'][name])


# Frozen 2b02be7 writer, independently pinned rather than derived from the
# candidate. The separate actual-module probe verifies this portable oracle.
_PRE_CANONICAL_EXTEND_SOURCE = '''def extend(service, room_id, audit_sha256, authorization, diagnosis, request_id):
    ao.identifier(request_id); _hash(audit_sha256, 'review-extension audit')
    ao.nonempty(authorization, 'authorization: actual new user answer and its approval context')
    ao.nonempty(diagnosis, 'diagnosis')
    inputs = {'request_id': request_id, 'audit_sha256': audit_sha256,
              'authorization': authorization, 'diagnosis': diagnosis}
    with service.locked(room_id) as (directory, state):
        committed = validate(service, state, allow_pending=True)
        if committed:
            record, _ = committed
            if record['inputs'] != inputs:
                raise RoomError('The one review extension already belongs to another payload; it cannot renew or renumber')
            return _result(state, record)
        pending = _pending(directory)
        if len(pending) > 1:
            raise RoomError('Multiple review-extension receipts exist; preserve them and diagnose')
        prior = _read(pending[0]) if pending else None
        if prior and (pending[0].name != request_id + '.json' or prior.get('inputs') != inputs):
            raise RoomError('An uncommitted review extension belongs to another payload')
        saved = _audit(directory, audit_sha256)
        evidence, _ = _inspect(service, directory, state, saved['evidence']['target'], reconcile=bool(prior))
        if evidence != saved['evidence']:
            raise RoomError('Review-extension audit is stale; charter, retained evidence or native metadata changed')
        record = {'version': 1, 'room_id': room_id, 'inputs': inputs, 'additional_spec_reviews': 1,
                  'maximum_spec_review_attempts': LIMIT + 1, 'before_state_sha256': evidence['state_sha256'],
                  'evidence_sha256': ao.digest(evidence), 'recorded_at': prior['recorded_at'] if prior else time.time()}
        if prior and prior != record:
            raise RoomError('Pending review-extension receipt differs from the recomputed grant')
        repeated, _ = _inspect(service, directory, state, evidence['target'], reconcile=bool(prior))
        if repeated != evidence:
            raise RoomError('Review-extension evidence changed before commit')
        relative = BASE + '/requests/' + request_id + '.json'
        if not prior:
            _store_once(directory / relative, record)
        state['spec_review_extension'] = {'request_id': request_id, 'key': ao.digest(inputs),
                                          'receipt': relative, 'receipt_sha256': ao.digest(record)}
        service.save(directory, state)
        return _result(state, record)
'''


class PendingCanonicalEvidenceTests(unittest.TestCase):
    def prepare(self, with_history=False, changed_audit=False, pending=True):
        fixture = RetainedHistoryTests('runTest') if with_history else ReviewExtensionFixture('runTest')
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        self.f = fixture.f if with_history else fixture
        self.with_history = with_history
        f = self.f
        audited = f.audit()
        saved = extension._audit(f.directory(), audited['audit_sha256'])
        if changed_audit:
            self.assertIs(type(saved['evidence']['state']['version']), int)
            saved['evidence']['state']['version'] = float(saved['evidence']['state']['version'])
            audited['audit_sha256'] = ao.digest(saved)
            ao.atomic(f.directory() / extension.BASE / 'audits' / (audited['audit_sha256'] + '.json'), saved)
        self.saved = saved
        self.inputs = f.grant_inputs(audit=audited)
        before = (f.directory() / 'state.json').read_bytes()
        posts = len(f.fake.posts)
        if pending:
            self.assertEqual(hashlib.sha256(_PRE_CANONICAL_EXTEND_SOURCE.encode()).hexdigest(),
                             '4b2c38cd2eae58a7aaef5dee74a0026479e9597c2dc9567d1d2a246dd461d722')
            namespace = dict(vars(extension))
            exec(compile(_PRE_CANONICAL_EXTEND_SOURCE, '<frozen-pre-repair-extend>', 'exec'), namespace)
            with (patch.object(extension, 'extend', namespace['extend']),
                  patch.object(f.service, 'save', side_effect=SystemExit('Synthetic old-writer process loss'))):
                with self.assertRaises(SystemExit):
                    f.service.ao_room_spec_review_extend(**self.inputs)
        self.assertEqual((f.directory() / 'state.json').read_bytes(), before)
        self.assertEqual(len(f.fake.posts), posts)
        f.service = ao.Service(f.home, lambda url: f.fake)
        self.paths = extension._pending(f.directory())
        self.assertEqual(len(self.paths), int(pending))
        full, _ = extension._inspect(f.service, f.directory(), f.state(), saved['evidence']['target'], reconcile=pending)
        self.assertEqual(full, saved['evidence'])  # The precise Python-equality fast path.
        self.assertEqual(ao.digest(full) != ao.digest(saved['evidence']), changed_audit)
        if pending:
            receipt = ao.read(self.paths[0])
            self.assertEqual(receipt['evidence_sha256'], ao.digest(full))
            self.assertEqual(receipt['evidence_sha256'] != ao.digest(saved['evidence']), changed_audit)

    def files(self):
        return {str(p.relative_to(self.f.directory())): p.read_bytes()
                for p in self.f.directory().rglob('*') if p.is_file()}

    def refuses(self, message='audit is stale'):
        before = self.files()
        posts = len(self.f.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, message):
            self.f.service.ao_room_spec_review_extend(**self.inputs)
        self.assertNotIn('spec_review_extension', self.f.state())
        self.assertEqual(extension._pending(self.f.directory()), self.paths)
        after = self.files()
        for name, raw in before.items():
            if name != 'state.json':
                self.assertEqual(after.get(name), raw, name)
        if not self.with_history:
            self.assertEqual(after, before)
        self.assertEqual(len(self.f.fake.posts), posts)

    def reconciles(self):
        before = self.files()
        posts = len(self.f.fake.posts)
        result = self.f.service.ao_room_spec_review_extend(**self.inputs)
        record, evidence = extension.validate(self.f.service, self.f.state())
        self.assertEqual(evidence, self.saved['evidence'])
        self.assertEqual(result['receipt_sha256'], ao.digest(ao.read(self.paths[0])))
        self.assertEqual(record['evidence_sha256'], ao.digest(self.saved['evidence']))
        self.assertEqual(result['remaining_spec_reviews'], 1)
        self.assertIs(result['model_dispatch'], False)
        self.assertEqual(self.f.service.ao_room_spec_review_extend(**self.inputs), result)
        after = self.files()
        self.assertEqual(set(before), set(after))
        self.assertEqual({k: v for k, v in before.items() if k != 'state.json'},
                         {k: v for k, v in after.items() if k != 'state.json'})
        self.assertEqual(len(self.f.fake.posts), posts)

    def test_old_pending_numeric_audit_without_history_refuses(self):
        self.prepare(changed_audit=True)
        self.refuses()

    def test_old_pending_numeric_audit_with_exact_modern_manifest_refuses(self):
        self.prepare(with_history=True, changed_audit=True)
        self.refuses()

    def test_numeric_audit_without_history_or_pending_receipt_refuses(self):
        self.prepare(changed_audit=True, pending=False)
        self.refuses()

    def test_numeric_modern_audit_without_pending_receipt_refuses(self):
        self.prepare(with_history=True, changed_audit=True, pending=False)
        self.refuses()

    def test_exact_old_pending_without_history_reconciles(self):
        self.prepare()
        self.reconciles()

    def test_exact_old_pending_with_modern_manifest_reconciles(self):
        self.prepare(with_history=True)
        self.reconciles()

    def test_second_full_inspection_requires_canonical_identity(self):
        self.prepare()
        inspect_now = extension._inspect
        calls = []
        def changed_type(*args, **kwargs):
            evidence, snapshots = inspect_now(*args, **kwargs)
            evidence = copy.deepcopy(evidence)
            if calls:
                # Isolate comparison hardening. This is not claimed to be a
                # reachable native producer race; real-file races remain above.
                evidence['state']['version'] = float(evidence['state']['version'])
            calls.append(evidence)
            return evidence, snapshots
        with patch.object(extension, '_inspect', side_effect=changed_type):
            self.refuses('evidence changed before commit')
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])
        self.assertNotEqual(ao.digest(calls[0]), ao.digest(calls[1]))


class HistoryRecordReads(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name).resolve() / 'room'; self.directory.mkdir()
        self.request = {'request_id': 'request'}
        self.row = {'version': 1, 'room_id': 'room', 'request_id': 'request', 'value': 'one'}
        self.sha = ao.digest(self.row)
        self.path = self.directory / 'outcomes' / 'request' / (self.sha + '.json')
        ao.atomic(self.path, self.row)

    def reader(self):
        return extension._HistoryEvidence(self.directory, 'synthetic-native-owner')

    def read(self):
        with self.reader() as evidence:
            return evidence.record('outcomes', self.request, self.sha)

    def test_duplicate_nonfinite_invalid_unicode_and_deep_json_refuse(self):
        for raw in (b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1e999}', b'{"x":"\\ud800"}',
                    b'{"x":"\xff"}', b'[' * 65 + b'0' + b']' * 65):
            with self.subTest(raw=raw):
                self.path.write_bytes(raw)
                with self.assertRaises(ao.RoomError): self.read()

    def test_leaf_parent_hardlink_fifo_and_foreign_ownership_refuse(self):
        import ao_evidence_audit_io as io
        raw = self.path.read_bytes(); target = self.directory / 'target'; target.write_bytes(raw)
        self.path.unlink(); self.path.symlink_to(target)
        with self.assertRaises(ao.RoomError): self.read()
        self.path.unlink(); os.link(target, self.path)
        with self.assertRaises(ao.RoomError): self.read()
        self.path.unlink(); os.mkfifo(self.path)
        with self.assertRaises(ao.RoomError): self.read()
        self.path.unlink(); self.path.write_bytes(raw)
        parent = self.path.parent; moved = self.directory / 'moved'; parent.rename(moved); parent.symlink_to(moved)
        with self.assertRaises(ao.RoomError): self.read()
        parent.unlink(); moved.rename(parent)
        with patch.object(io, '_uid', return_value=os.getuid() + 1), self.assertRaises(ao.RoomError): self.read()

    def test_cached_record_is_rechecked_on_exit(self):
        with self.assertRaises(ao.RoomError):
            with self.reader() as evidence:
                evidence.record('outcomes', self.request, self.sha)
                self.path.write_bytes(b'{}\n')
                self.assertEqual(evidence.record('outcomes', self.request, self.sha), self.row)

    def test_actual_descriptor_read_stays_at_original_extent_under_growth(self):
        import ao_evidence_audit_io as io
        original = io.Root.open_file; positions = []
        size = self.path.stat().st_size
        close = io.Opened.close
        def open_and_grow(root, *args):
            opened = original(root, *args)
            with self.path.open('ab') as stream: stream.write(b' ' * 10000)
            return opened
        def capture(opened):
            positions.append(opened.handle.tell()); close(opened)
        with patch.object(io.Root, 'open_file', open_and_grow), patch.object(io.Opened, 'close', capture):
            with self.reader() as evidence:
                with self.assertRaises(ao.RoomError): evidence.record('outcomes', self.request, self.sha)
                self.assertEqual(evidence.total, size)
        self.assertEqual(positions, [size])

    def test_record_and_aggregate_admission_refuse_before_any_read(self):
        import ao_evidence_audit_io as io
        original = os.fdopen; calls = []
        def tracked(*args, **kwargs):
            calls.append(args[0]); return original(*args, **kwargs)
        for field in ('MAX_HISTORY_RECORD_BYTES', 'MAX_HISTORY_TOTAL_BYTES'):
            with self.subTest(field=field), patch.object(extension, field, 1), patch.object(io.os, 'fdopen', tracked):
                with self.reader() as evidence:
                    with self.assertRaises(ao.RoomError): evidence.record('outcomes', self.request, self.sha)
                    self.assertEqual(evidence.total, 0)
        self.assertEqual(calls, [])

    def test_frozen_limits_and_cycle_guard_are_independent_of_record_authentication(self):
        self.assertEqual(extension.MAX_HISTORY_RECORD_BYTES, 8 * 1024 * 1024)
        self.assertEqual(extension.MAX_HISTORY_TOTAL_BYTES, 64 * 1024 * 1024)
        self.assertEqual(extension.MAX_HISTORY_INVALIDATIONS, 1000)
        head, proof = 'a' * 64, 'b' * 64
        # A true content-addressed hash cycle cannot be manufactured here. Isolate
        # traversal's cycle guard; real record authentication has separate probes.
        with self.reader() as evidence, patch.object(evidence, 'record', return_value={
                'kind': 'invalidation', 'proof_sha256': proof, 'previous_sha256': head}) as record:
            with self.assertRaises(ao.RoomError):
                evidence.lineage({**self.request, history.PROOF: proof, history.INVALIDATION: head}, {})
            self.assertEqual(record.call_count, 1)


if __name__ == '__main__':
    unittest.main()
