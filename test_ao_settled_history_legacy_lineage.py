"""Legacy failed-turn proof ancestors in a renewed settled-history lineage.

Proofs minted before the saved-error anchor existed (pre-df27994, #176) carry exactly the 14
legacy input keys and bind ``ao_terminal_sha256`` to the RAW observed terminal row, including
its ``errorMessage``. The modern minting keeps the same proof version and kind but adds
``saved_error_message_sha256`` and hashes only the normalized terminal identity. After an
upgrade an explicit outcome audit invalidates the legacy proof and re-mints a modern one, so
retained rooms hold a lineage whose SUPERSEDED ancestors are legacy-shaped while its current
head is modern.

``_HistoryEvidence`` admits that lineage: the head must have the modern key set; every proof
reached through a validated invalidation edge may have either exact shape, with legacy proofs
compared against the raw retained terminal and modern proofs against the normalized identity.
A legacy-shaped head, any missing or extra key and every anchor failure still refuse.

Synthetic data only: no real user names, machine paths, room ids or private context. The legacy
minting implementation below is the frozen pre-df27994 source, pinned by digest and executed
only inside minting patches; the installed validator under test is never replaced.
"""
import copy
import hashlib
import inspect
import json
import unittest
from unittest.mock import patch

import ao_history_reconciliation as reconciliation
import ao_native_outcome as native_outcome
import ao_project_room as ao
import ao_review_extension as extension
from test_ao_preacceptance_review_extension import PreAcceptanceFixture


# Frozen df2799412c6d2e98a8348f0966751aac7d590629^:ao_history_reconciliation.py#_failed_inputs,
# independently pinned rather than derived from the installed module; its inner imports resolve
# against the current modules, exactly like _PRE_CANONICAL_EXTEND_SOURCE in
# test_ao_review_extension_history.py.
_LEGACY_FAILED_INPUTS_SOURCE = '''def _failed_inputs(state, request, saved, observed, snapshot, native):
    """Proof inputs for an uncertain failed turn whose saved receipt was truncated.

    The failed-turn proof keeps the settled request's evidence loadable: a settled
    request must still validate the same immutable inputs against a fresh complete
    observation. Minting remains reachable only through the uncertain state because
    settlement itself requires the proof.
    """
    from ao_project_room import busy, conflicting_reroute, digest, sent_message
    from ao_native_outcome import source_keys
    from ao_outcomes import classify, _unowned_context_turns
    saved_turn = saved.get('turn')
    observed_turn = observed.get('turn')
    if (request.get('state') not in ('uncertain', 'settled_failure')
            or saved.get('history_truncated') is not True
            or not isinstance(saved_turn, dict) or not isinstance(observed_turn, dict)
            or saved_turn.get('id') != request.get('turn_id')
            or saved_turn.get('providerTurnId') != request.get('provider_turn_id')
            or saved_turn.get('state') != 'failed'
            or observed_turn.get('id') != saved_turn.get('id')
            or observed_turn.get('providerTurnId') != saved_turn.get('providerTurnId')
            or observed_turn.get('state') != 'failed'
            or observed_turn.get('errorMessage') != saved_turn.get('errorMessage')
            or snapshot.get('history_truncated') is not False
            or busy(snapshot) or not sent_message(request, snapshot)
            or request.get('model_reroute') or request.get('conflicting_reroute')
            or conflicting_reroute(request, snapshot)
            or not isinstance(native, dict) or native.get('unknown')
            or not isinstance(native.get('anchor_uuid'), str) or not native['anchor_uuid']
            or native.get('next_human_uuid') is not None):
        return None
    source = native.get('source')
    source_key, _ = source_keys(request['role'])
    errors = native.get('errors')
    if (not isinstance(source, dict) or source != state.get(source_key)
            or source.get('session_id') != request['session_id']
            or not isinstance(native.get('source_sha256'), str)
            or not re.fullmatch('[0-9a-f]{64}', native['source_sha256'])
            or not isinstance(errors, list) or any(not isinstance(e, dict) for e in errors)
            or not any(e.get('error') == 'rate_limit' or e.get('http_status') == 429 for e in errors)):
        return None
    baseline_turns = set(request['baseline']['turn_ids'])
    current_turns = {turn['id'] for turn in snapshot['turns']}
    proven_context = _unowned_context_turns(state, {
        item['turn_id'] for field in ('compaction_imports', 'task_notification_imports')
        for item in native.get(field, [])})
    if (not baseline_turns <= current_turns
            or current_turns - baseline_turns - {request['turn_id']} - proven_context):
        return None
    # The derived complete-history view never replaces the original receipt,
    # its bytes, its turn rows or its usage.
    if classify({**observed, 'history_truncated': False}, native)['kind'] != 'quota_limit':
        return None
    return {'session_id': request['session_id'], 'role': request['role'],
            'turn_id': request['turn_id'], 'provider_turn_id': request['provider_turn_id'],
            'conversation_id': snapshot.get('conversationId'), 'branch_id': snapshot.get('activeBranchId'),
            'baseline_sha256': digest(request['baseline']), 'messages_sha256': digest(saved['messages']),
            'turns_sha256': digest(snapshot['turns']), 'ao_terminal_sha256': digest(observed['turn']),
            'saved_turn_sha256': digest(saved_turn),
            'provider_failures_sha256': digest(observed['provider_failures']),
            'session_failures_sha256': digest(observed['sessionFailures']), 'native': native}
'''

_LEGACY_FAILED_INPUTS_SHA256 = '2f3d0fe058cdedb7030b23b0e96b94173fcc2877ad421dea37247024ea2a6731'

# Local mirrors of the two exact failed-turn input key sets; the shape test cross-checks them
# against the installed validator's constants.
LEGACY_KEYS = frozenset({'session_id', 'role', 'turn_id', 'provider_turn_id', 'conversation_id',
                         'branch_id', 'baseline_sha256', 'messages_sha256', 'turns_sha256',
                         'ao_terminal_sha256', 'saved_turn_sha256', 'provider_failures_sha256',
                         'session_failures_sha256', 'native'})
MODERN_KEYS = LEGACY_KEYS | {'saved_error_message_sha256'}

_REQUEST = 'truncated-failure'
_RESUME = 'truncated-resume'
_INCONSISTENT = 'Review-extension retained history evidence is inconsistent'


def _oracle(source, label='<frozen-legacy-failed-inputs>'):
    """Execute one frozen minting source into the reconciliation module's namespace."""
    namespace = dict(vars(reconciliation))
    exec(compile(source, label, 'exec'), namespace)
    return namespace['_failed_inputs']


def _legacy_oracle():
    return _oracle(_LEGACY_FAILED_INPUTS_SOURCE)


def _source_variant(source, old, new, label):
    """One minting source with exactly one expression changed; still a real mint, not an edit."""
    if old not in source:
        raise ValueError('variant anchor not found in source')
    return _oracle(source.replace(old, new, 1), label)


class LegacyLineageFixture(PreAcceptanceFixture):
    """Five failed-turn proofs, four invalidations and the referenced outcome rows.

    The RenewedTruncatedSettledFailureFixture recipe, iterated: each explicit outcome audit
    re-mints the proof for the still-uncertain failed turn, and each refused premature send
    durably invalidates the current head out-of-band. ``mint_oracle`` selects the minting
    implementation per proof so P1/P2 carry the frozen legacy input shape and P3..P5 the modern
    one, exactly as an upgrade through #176 leaves retained history. ``tail_oracle`` stays
    installed across resume and the correction sync, so a test can keep the current head
    legacy-shaped through settlement.
    """

    PROVIDER_TEXT = 'API Error: Rate limit reached'

    def native(self, sha):
        return {'anchor_uuid': 'owned-native-packet-truncated-failure', 'next_human_uuid': None,
                'errors': [{'uuid': 'native-quota-truncated-failure', 'error': 'rate_limit', 'http_status': 429}],
                'stop_reasons': [], 'source_sha256': sha, 'source': self.state()['native_outcome_source']}

    def mint_oracle(self, index):
        """The minting implementation for proof P<index+1>; P1/P2 legacy, P3..P5 modern."""
        return _legacy_oracle() if index < 2 else reconciliation._failed_inputs

    def tail_oracle(self):
        return reconciliation._failed_inputs

    def setUp(self):
        super().setUp()
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.send('implementation', _REQUEST)
        snapshot = self.fake.snapshots['engineer']
        self.fake.finish('engineer', state='failed')
        self.failed_turn = snapshot['turns'][-1]
        if self.PROVIDER_TEXT is not None:
            self.failed_turn['errorMessage'] = self.PROVIDER_TEXT
        snapshot['history_truncated'] = True
        self.service.ao_room_sync(self.room)
        request = self.state()['requests'][_REQUEST]
        self.assertEqual(request['state'], 'uncertain')
        receipt = ao.read(self.directory() / request['receipt'])
        self.assertTrue(receipt['history_truncated'])
        snapshot['history_truncated'] = False
        self.proofs, self.invalidations, self.outcomes, self.mint_args = [], [], [], []
        premature = 0
        with patch.object(native_outcome, 'inspect', return_value=self.native('e' * 64)):
            for index in range(5):
                oracle = self.mint_oracle(index)

                def spy(*args, _oracle=oracle):
                    self.mint_args.append(copy.deepcopy(args))
                    return _oracle(*args)

                with patch.object(reconciliation, '_failed_inputs', spy):
                    audited = self.service.ao_room_outcome_audit(self.room)
                self.outcomes.append(audited['outcome_sha256'])
                request = self.state()['requests'][_REQUEST]
                self.proofs.append(request[reconciliation.PROOF])
                if index + 1 < 5:
                    # The refused send propagates unmodified; the lock's own except clause writes
                    # the out-of-band invalidation (see RenewedTruncatedSettledFailureFixture).
                    premature += 1
                    posts = len(self.fake.posts)
                    with self.assertRaisesRegex(
                            ao.RoomError,
                            'An owned request is active or uncertain; sync it without resending'):
                        self.send('correction', 'premature-' + str(premature))
                    self.assertEqual(len(self.fake.posts), posts)
                    request = self.state()['requests'][_REQUEST]
                    self.invalidations.append(request[reconciliation.INVALIDATION])
            with patch.object(reconciliation, '_failed_inputs', self.tail_oracle()):
                self.service.ao_room_outcome_resume(
                    self.room, _REQUEST, self.outcomes[-1], _RESUME,
                    'Native quota rejection identified at the truncated turn; user confirmed availability.',
                    'User approved the exact one-time continuation for the truncated turn.')
                self.assertEqual(self.state()['requests'][_REQUEST]['state'], 'settled_failure')
                self.send('correction', _RESUME)
        self.fake.finish('engineer', json.dumps(self.scope_change_report()))
        self.service.ao_room_sync(self.room)
        self.final_request_id = _RESUME
        self.assertEqual(self.state()['requests'][self.final_request_id]['state'], 'completed')
        self.register()
        self.assert_lineage_shape()

    def record(self, area, sha256):
        return ao.read(self.directory() / area / _REQUEST / (sha256 + '.json'))

    def proof_record(self, sha256):
        return self.record('history-reconciliations', sha256)

    def expected_key_set(self, index):
        """The input key set proof P<index+1> must carry; None where a variant minted it."""
        return LEGACY_KEYS if index < 2 else MODERN_KEYS

    def assert_lineage_shape(self):
        self.assertEqual(len(set(self.proofs)), 5)
        self.assertEqual(len(set(self.invalidations)), 4)
        for index, sha256 in enumerate(self.proofs):
            proof = self.proof_record(sha256)
            self.assertEqual(proof['kind'], 'complete_history_native_quota_failed_turn')
            expected = self.expected_key_set(index)
            if expected is not None:
                self.assertEqual(set(proof['inputs']), expected, 'P' + str(index + 1))
        edges = [self.record('history-reconciliation-invalidations', sha256)
                 for sha256 in self.invalidations]
        for index, edge in enumerate(edges):
            self.assertEqual(edge['proof_sha256'], self.proofs[index])
            self.assertEqual(edge['previous_sha256'],
                             self.invalidations[index - 1] if index else None)
            self.assertRegex(edge.get('outcome_sha256') or '', '^[0-9a-f]{64}$')
        self.assertIsNone(self.proof_record(self.proofs[0]).get('invalidation_sha256'))
        for index in range(1, 5):
            self.assertEqual(self.proof_record(self.proofs[index])['invalidation_sha256'],
                             self.invalidations[index - 1])
        # Referenced outcomes: each invalidation names the audited outcome of the proof it
        # invalidated; each proof supersedes the outcome current when it was minted.
        referenced = {self.record('history-reconciliation-invalidations', sha256)['outcome_sha256']
                      for sha256 in self.invalidations}
        referenced.update(proof['supersedes_outcome_sha256'] for sha256 in self.proofs
                          for proof in [self.proof_record(sha256)]
                          if proof.get('supersedes_outcome_sha256'))
        self.assertEqual(len(self.invalidations), 4)
        self.referenced_outcomes = referenced

    def audit(self, **changes):
        changes.setdefault('scope_request_id', self.final_request_id)
        if 'candidate_sha256' not in changes:
            changes['candidate_sha256'] = self.candidate_sha256(self.final_request_id)
        return super().audit(**changes)

    def saved_audit(self, audit_sha256):
        return ao.read(self.directory() / extension.BASE / 'audits' / (audit_sha256 + '.json'))

    def reconciliation_path(self, sha256):
        return 'history-reconciliations/' + _REQUEST + '/' + sha256 + '.json'

    def invalidation_path(self, sha256):
        return 'history-reconciliation-invalidations/' + _REQUEST + '/' + sha256 + '.json'

    def outcome_path(self, sha256):
        return 'outcomes/' + _REQUEST + '/' + sha256 + '.json'

    def assert_refuses_preserved(self, action, pattern=_INCONSISTENT):
        """``action`` refuses, makes no AO POST, and mutates no original retained record.

        ``state.json`` may change and one more invalidation row may be appended: the lock's own
        except clause records the refusal as an out-of-band invalidation of the latest proof.
        """
        before = self.room_files()
        posts = len(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, pattern):
            action()
        self.assertEqual(len(self.fake.posts), posts)
        after = self.room_files()
        for name, raw in before.items():
            if name != 'state.json':
                self.assertEqual(after.get(name), raw, name)
        added = set(after) - set(before)
        self.assertTrue(all(name.startswith('history-reconciliation-invalidations/') for name in added),
                        str(added))

    def captured_inputs(self):
        """The minting arguments recorded for the last minted proof."""
        return self.mint_args[-1]


class MixedLineageTests(LegacyLineageFixture):
    def setUp(self):
        super().setUp()
        self.assertEqual(len(self.referenced_outcomes), 4)
        # The four audited outcomes the invalidation edges name; each proof's supersedes link
        # names the same row the preceding edge already referenced.

    def test_lineage_key_sets_and_raw_terminal_digests(self):
        legacy, modern = 0, 0
        for index, sha256 in enumerate(self.proofs):
            proof = self.proof_record(sha256)
            if set(proof['inputs']) == LEGACY_KEYS:
                legacy += 1
                self.assertNotIn('saved_error_message_sha256', proof['inputs'])
            else:
                modern += 1
        self.assertEqual((legacy, modern), (2, 3))
        # At least one legacy-claimed outcome row keeps the raw provider text, so its raw
        # terminal digest differs from the normalized identity a legacy proof binds.
        claimed_raw_drift = False
        for sha256 in self.invalidations[:2]:
            edge = self.record('history-reconciliation-invalidations', sha256)
            outcome = self.record('outcomes', edge['outcome_sha256'])
            terminal = outcome['ao_terminal']
            self.assertEqual(terminal.get('errorMessage'), self.PROVIDER_TEXT)
            proof = self.proof_record(edge['proof_sha256'])
            raw = ao.digest(terminal)
            self.assertEqual(proof['inputs']['ao_terminal_sha256'], raw)
            if raw != ao.digest(reconciliation._terminal_identity(terminal)):
                claimed_raw_drift = True
        self.assertTrue(claimed_raw_drift)

    def test_full_cycle_admits_grant_and_consumes_exactly_one_post(self):
        before = self.room_files()
        posts = len(self.fake.posts)
        audit = self.audit()
        self.assertTrue(audit['eligible'])
        saved = self.saved_audit(audit['audit_sha256'])
        manifest = saved['evidence']['manifest']
        for sha256 in self.proofs:
            self.assertIn(self.reconciliation_path(sha256), manifest)
        for sha256 in self.invalidations:
            self.assertIn(self.invalidation_path(sha256), manifest)
        for name, raw in before.items():
            if name != 'state.json':
                self.assertEqual(self.room_files().get(name), raw, name)
        self.assertEqual(len(self.fake.posts), posts)
        self.grant()
        extension.validate(self.service, self.state())
        self.assertEqual(self.service.ao_room_status(self.room)['spec_review_extension']
                         ['remaining_spec_reviews'], 1)
        # A controller restart re-validates the same retained lineage on fresh objects.
        self.service = ao.Service(self.home, lambda url: self.fake)
        extension.validate(self.service, self.state())
        self.service.ao_room_status(self.room)
        self.send('spec_review', 'fourth')
        self.assertEqual(len(self.fake.posts), posts + 1)
        extension.validate(self.service, self.state())
        self.assertNotIn('fifth', self.state()['requests'])

    def test_lost_ack_fourth_send_refuses_a_second_send_without_resending(self):
        self.grant()
        posts = len(self.fake.posts)
        self.fake.lose_ack = True
        try:
            sent = self.send('spec_review', 'fourth')
        finally:
            self.fake.lose_ack = False
        self.assertEqual(sent['state'], 'uncertain')
        self.assertEqual(len(self.fake.posts), posts + 1)
        self.assertEqual(self.state()['requests']['fourth']['state'], 'uncertain')
        with self.assertRaisesRegex(
                ao.RoomError, 'An owned request is active or uncertain; sync it without resending'):
            self.send('spec_review', 'fourth-again')
        self.assertEqual(len(self.fake.posts), posts + 1)
        self.assertNotIn('fourth-again', self.state()['requests'])


class EmptyErrorLineageFixture(LegacyLineageFixture):
    """The same five-proof lineage with no errorMessage on the failed turn: a legacy raw
    terminal digest equals the normalized identity, so shape alone must carry admission."""

    PROVIDER_TEXT = None

    def setUp(self):
        super().setUp()
        # Raw and normalized digests are equal for every legacy-claimed outcome row here.
        for sha256 in self.invalidations[:2]:
            edge = self.record('history-reconciliation-invalidations', sha256)
            terminal = self.record('outcomes', edge['outcome_sha256'])['ao_terminal']
            self.assertNotIn('errorMessage', terminal)
            proof = self.proof_record(edge['proof_sha256'])
            self.assertEqual(proof['inputs']['ao_terminal_sha256'], ao.digest(terminal))
            self.assertEqual(ao.digest(terminal),
                             ao.digest(reconciliation._terminal_identity(terminal)))


class EmptyErrorLineageTests(EmptyErrorLineageFixture):
    def test_no_error_message_legacy_lineage_admits_the_full_cycle(self):
        audit = self.audit()
        self.assertTrue(audit['eligible'])
        self.grant()
        posts = len(self.fake.posts)
        self.send('spec_review', 'fourth')
        self.assertEqual(len(self.fake.posts), posts + 1)


class OracleParityTests(unittest.TestCase):
    """The frozen oracle reproduces the pre-#176 minting exactly."""

    def args(self, provider_text):
        fixture = LegacyLineageFixture('runTest')
        fixture.PROVIDER_TEXT = provider_text
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        return fixture.captured_inputs(), fixture

    def test_pinned_oracle_source_digest(self):
        self.assertEqual(hashlib.sha256(_LEGACY_FAILED_INPUTS_SOURCE.encode()).hexdigest(),
                         _LEGACY_FAILED_INPUTS_SHA256)

    def test_oracle_equals_modern_minus_the_error_anchor_with_raw_hashes(self):
        args, _ = self.args('API Error: Rate limit reached')
        legacy = _legacy_oracle()(*copy.deepcopy(args))
        modern = reconciliation._failed_inputs(*copy.deepcopy(args))
        self.assertEqual(set(legacy), LEGACY_KEYS)
        self.assertEqual(set(modern), MODERN_KEYS)
        state, request, saved, observed, snapshot, native = args
        self.assertEqual(legacy['turns_sha256'], ao.digest(snapshot['turns']))
        self.assertEqual(legacy['ao_terminal_sha256'], ao.digest(observed['turn']))
        changed = {key for key in legacy if legacy[key] != modern[key]}
        self.assertEqual(changed, {'turns_sha256', 'ao_terminal_sha256'})
        self.assertNotEqual(legacy['ao_terminal_sha256'],
                            ao.digest(reconciliation._terminal_identity(observed['turn'])))

    def test_oracle_hashes_equal_modern_when_no_error_message(self):
        args, _ = self.args(None)
        legacy = _legacy_oracle()(*copy.deepcopy(args))
        modern = reconciliation._failed_inputs(*copy.deepcopy(args))
        self.assertEqual({key: value for key, value in modern.items()
                          if key != 'saved_error_message_sha256'}, legacy)


class _VariantFixture(LegacyLineageFixture):
    """Base for negatives minting one proof under a tampered minting variant.

    ``VARIANT_KEY_SET`` names the key set the variant-minted proof still carries; None when the
    variant itself changes the shape, so the shape check verifies the untampered proofs only.
    """

    VARIANT_INDEX = 0
    VARIANT_KEY_SET = None

    def variant(self):
        raise NotImplementedError

    def mint_oracle(self, index):
        return self.variant() if index == self.VARIANT_INDEX else super().mint_oracle(index)

    def expected_key_set(self, index):
        return self.VARIANT_KEY_SET if index == self.VARIANT_INDEX else super().expected_key_set(index)


def _legacy_variant(old, new, index=0):
    """A fixture class minting one legacy proof with one source expression changed."""
    class Fixture(_VariantFixture):
        VARIANT_INDEX = index

        def variant(self):
            return _source_variant(_LEGACY_FAILED_INPUTS_SOURCE, old, new,
                                   '<variant-legacy-failed-inputs>')
    return Fixture


def _modern_variant(old, new, index=2):
    class Fixture(_VariantFixture):
        VARIANT_INDEX = index

        def variant(self):
            return _source_variant(inspect.getsource(reconciliation._failed_inputs),
                                   old, new, '<variant-modern-failed-inputs>')
    return Fixture


class ForgedLegacySavedTurnTests(_legacy_variant(
        "'saved_turn_sha256': digest(saved_turn)",
        "'saved_turn_sha256': digest({**saved_turn, 'forged': True})")):
    def test_changed_legacy_saved_turn_anchor_refuses(self):
        self.assert_refuses_preserved(self.audit)


class ForgedLegacyConversationIdTests(_legacy_variant(
        "'conversation_id': snapshot.get('conversationId')",
        "'conversation_id': snapshot.get('conversationId') + '-forged'")):
    def test_changed_legacy_identity_anchor_refuses(self):
        self.assert_refuses_preserved(self.audit)


class ForgedLegacyMessagesTests(_legacy_variant(
        "'messages_sha256': digest(saved['messages'])",
        "'messages_sha256': digest(saved['messages'] + [{'id': 'forged'}])")):
    def test_changed_legacy_messages_anchor_refuses(self):
        self.assert_refuses_preserved(self.audit)


class ForgedLegacyTerminalTests(_legacy_variant(
        "'ao_terminal_sha256': digest(observed['turn'])",
        "'ao_terminal_sha256': digest({**observed['turn'], 'errorMessage': 'different provider text'})")):
    def test_changed_legacy_raw_terminal_refuses(self):
        self.assert_refuses_preserved(self.audit)


class MasqueradingModernFixture(_VariantFixture):
    """A minted ancestor carrying modern normalized hashes under the legacy key set.

    It binds ao_terminal_sha256 to the stripped identity but omits the saved-error key, so it
    reads as legacy; the raw-terminal rule then refuses it against the retained outcome row's
    errorMessage-bearing terminal -- no fallback from the legacy rule to the modern one.
    """

    VARIANT_KEY_SET = LEGACY_KEYS

    def variant(self):
        source = inspect.getsource(reconciliation._failed_inputs)
        anchor = "            'saved_error_message_sha256': digest(saved_turn.get('errorMessage')),\n"
        return _source_variant(source, anchor, '', '<masquerading-failed-inputs>')


class MasqueradingModernTests(MasqueradingModernFixture):
    def test_modern_hashes_under_legacy_keys_refuse(self):
        proof = self.proof_record(self.proofs[0])
        self.assertEqual(set(proof['inputs']), LEGACY_KEYS)
        edge = self.record('history-reconciliation-invalidations', self.invalidations[0])
        self.assertEqual(edge['proof_sha256'], self.proofs[0])
        terminal = self.record('outcomes', edge['outcome_sha256'])['ao_terminal']
        self.assertEqual(proof['inputs']['ao_terminal_sha256'],
                         ao.digest(reconciliation._terminal_identity(terminal)))
        self.assertNotEqual(proof['inputs']['ao_terminal_sha256'], ao.digest(terminal))
        self.assert_refuses_preserved(self.audit)


class ForgedModernSavedErrorTests(_modern_variant(
        "digest(saved_turn.get('errorMessage'))", "digest('forged provider text')")):
    def test_modern_ancestor_with_wrong_saved_error_refuses(self):
        self.assert_refuses_preserved(self.audit)


class ExtraKeyModernTests(_modern_variant(
        "'saved_error_message_sha256': digest(saved_turn.get('errorMessage'))",
        "'saved_error_message_sha256': digest(saved_turn.get('errorMessage')), 'forged_extra': 'x'")):
    def test_modern_ancestor_with_an_extra_key_refuses(self):
        proof = self.proof_record(self.proofs[2])
        self.assertIn('forged_extra', proof['inputs'])
        self.assert_refuses_preserved(self.audit)


class LegacyHeadFixture(LegacyLineageFixture):
    """The final mint stays legacy-shaped: the upgrade never reached this room's head.

    ao_quality_review's own send-time inspector also validates the current head's modern anchor
    (its own, narrower check that never walks ancestors), so it would refuse the correction send
    before this fixture can settle. That gate is a different lane under test elsewhere; here only
    its exact result shape is patched for that one send so the lineage validator sees the genuine
    legacy head the way a retained room would present it to the extension audit.
    """

    def mint_oracle(self, index):
        return _legacy_oracle() if index in (0, 1, 4) else reconciliation._failed_inputs

    def tail_oracle(self):
        return _legacy_oracle()

    def expected_key_set(self, index):
        return LEGACY_KEYS if index in (0, 1, 4) else MODERN_KEYS

    def send(self, purpose, key=None):
        if key == _RESUME:
            minimal = {'source': None, 'pending': [], 'equivalence': None,
                       'equivalent_amendment_sha256': []}
            with patch('ao_quality_review.inspect', return_value=minimal):
                return super().send(purpose, key)
        return super().send(purpose, key)


class LegacyHeadTests(LegacyHeadFixture):
    def test_legacy_shaped_current_proof_refuses(self):
        proof = self.proof_record(self.proofs[-1])
        self.assertEqual(set(proof['inputs']), LEGACY_KEYS)
        self.assert_refuses_preserved(self.audit)


class MissingEvidenceTests(LegacyLineageFixture):
    def test_missing_legacy_ancestor_refuses(self):
        (self.directory() / self.reconciliation_path(self.proofs[0])).unlink()
        self.assert_refuses_preserved(self.audit, 'unavailable, unsafe or over its bound')

    def test_missing_invalidation_refuses(self):
        (self.directory() / self.invalidation_path(self.invalidations[0])).unlink()
        self.assert_refuses_preserved(self.audit, 'unavailable, unsafe or over its bound')

    def test_missing_referenced_outcome_refuses(self):
        edge = self.record('history-reconciliation-invalidations', self.invalidations[0])
        (self.directory() / self.outcome_path(edge['outcome_sha256'])).unlink()
        self.assert_refuses_preserved(self.audit, 'unavailable, unsafe or over its bound')

    def test_altered_referenced_outcome_refuses(self):
        edge = self.record('history-reconciliation-invalidations', self.invalidations[0])
        path = self.directory() / self.outcome_path(edge['outcome_sha256'])
        row = json.loads(path.read_text())
        row['ao_terminal']['errorMessage'] = 'altered provider text'
        path.write_text(json.dumps(row, sort_keys=True) + '\n')
        self.assert_refuses_preserved(self.audit)

    def test_reserialized_referenced_outcome_refuses_after_the_grant(self):
        self.grant()
        edge = self.record('history-reconciliation-invalidations', self.invalidations[0])
        path = self.directory() / self.outcome_path(edge['outcome_sha256'])
        raw = path.read_bytes()
        row = json.loads(raw)
        path.write_text(json.dumps(row, indent=2, sort_keys=True) + '\n')
        posts = len(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, 'missing or modified'):
            extension.validate(self.service, self.state())
        with self.assertRaisesRegex(ao.RoomError, 'missing or modified'):
            self.service.ao_room_status(self.room)
        with self.assertRaisesRegex(ao.RoomError, 'missing or modified'):
            self.send('spec_review', 'fourth')
        self.assertEqual(len(self.fake.posts), posts)


class LiveDriftTests(LegacyLineageFixture):
    def test_live_error_message_drift_refuses(self):
        self.failed_turn['errorMessage'] = 'a different provider text'
        self.assert_refuses_preserved(
            self.audit, 'Settled native failure live error contradicts its settled receipt')

    def test_live_state_drift_refuses(self):
        self.failed_turn['state'] = 'completed'
        self.assert_refuses_preserved(
            self.audit, 'Settled native failure live state contradicts its settled receipt')


class PostGrantDriftTests(LegacyLineageFixture):
    def test_deleted_legacy_ancestor_after_grant_blocks_validation_and_send(self):
        self.grant()
        posts = len(self.fake.posts)
        (self.directory() / self.reconciliation_path(self.proofs[0])).unlink()
        with self.assertRaisesRegex(ao.RoomError, 'unavailable, unsafe or over its bound'):
            extension.validate(self.service, self.state())
        with self.assertRaisesRegex(ao.RoomError, 'unavailable, unsafe or over its bound'):
            self.service.ao_room_status(self.room)
        with self.assertRaisesRegex(ao.RoomError, 'unavailable, unsafe or over its bound'):
            self.send('spec_review', 'fourth')
        self.assertEqual(len(self.fake.posts), posts)
        self.assertNotIn('fourth', self.state()['requests'])
