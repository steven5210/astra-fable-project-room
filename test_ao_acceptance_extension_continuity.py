"""Native owner continuity across acceptance-review continuation grants (review-4).

Confirmed defect: `_inspect` computed each fresh audit's native owners only against the
explicit read-only AO owner database and the AO binding; nothing compared those owners
with the native ownership already frozen by earlier committed grants in the same room's
continuation chain. A caller could therefore replace either role's actual provider
conversation in the AO ownership tables (both `sessions` and `conversation_branches`)
between a consumed grant and the next audit, and the next audit, extend and dispatch would
silently legitimize the replacement -- see jobs/acceptance-continuation/review-4 for the
rejection review and its reproducer probe. This file adds regression coverage for the
correction (`_retained_owners` plus its `_inspect` integration in ao_acceptance_extension.py)
while preserving successful same-native controller restarts and legitimate engineering
corrections made between grants.

This file imports only `AcceptanceContinuationFixture` from `test_ao_acceptance_extension`
so the existing test classes there are not collected twice. The small snapshot/preservation
helpers below are copied locally (matching `test_ao_acceptance_extension_matrix.MatrixFixture`)
for the same reason, rather than imported. Every scenario uses only synthetic offline state
already produced by the base fixture; no network or provider access.
"""
import copy
import json
import os
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

import ao_acceptance_extension as extension
import ao_project_room as ao
from test_ao_acceptance_extension import AcceptanceContinuationFixture


class NativeOwnerContinuityFixture(AcceptanceContinuationFixture):
    """Local copies of the matrix snapshot helpers (see test_ao_acceptance_extension_matrix.py),
    plus the replacement/restoration/chain helpers this file's scenarios need."""

    maxDiff = None

    # --- copied locally from test_ao_acceptance_extension_matrix.MatrixFixture ---

    def lane_root(self):
        return self.directory() / extension.BASE

    def journal_dir(self):
        return self.lane_root() / 'journal'

    def lane_snapshot(self):
        base = self.lane_root()
        result = {}
        if not base.exists() and not base.is_symlink():
            return result
        for root, dirs, files in os.walk(base, followlinks=False):
            for name in dirs + files:
                path = Path(root) / name
                rel = str(path.relative_to(base))
                if path.is_symlink():
                    result[rel] = b'symlink:' + os.readlink(path).encode()
                elif path.is_dir():
                    result[rel] = b'dir'
                elif path.is_file():
                    result[rel] = path.read_bytes()
        return result

    def journal_bytes(self):
        if not self.journal_dir().exists():
            return {}
        return {name: (self.journal_dir() / name).read_bytes()
                for name in sorted(os.listdir(self.journal_dir()))
                if (self.journal_dir() / name).is_file()}

    def room_files(self):
        result = {}
        for path in self.directory().rglob('*'):
            if path.is_file() and not path.is_symlink() and path.name != 'state.json':
                result[str(path.relative_to(self.directory()))] = path.read_bytes()
        return result

    def assert_preserved(self, snapshot):
        for relative, data in snapshot.items():
            path = self.directory() / relative
            self.assertTrue(path.is_file(), relative)
            self.assertEqual(path.read_bytes(), data, relative)

    def sql(self, statement, params=()):
        with sqlite3.connect(self.database) as database:
            database.execute(statement, params)

    # --- new helpers for this file's scenarios ---

    def replace(self, role, provider_id, generation=None):
        """Mutate only the actual native/vendor conversation identity (and, optionally, the
        controller generation) for `role` in BOTH ownership tables, exactly reproducing the
        confirmed defect scenario. The AO session id (the sqlite primary key) never changes."""
        if generation is None:
            self.sql('UPDATE sessions SET provider_conversation_id=? WHERE id=?', (provider_id, role))
        else:
            self.sql('UPDATE sessions SET provider_conversation_id=?, controller_generation=? WHERE id=?',
                     (provider_id, generation, role))
        self.sql('UPDATE conversation_branches SET provider_conversation_id=? WHERE session_id=?',
                 (provider_id, role))

    def restore(self, role):
        """Undo `replace`, restoring the retained native conversation the base fixture created."""
        self.replace(role, 'synthetic-native-' + role, 'generation')

    def consumed_fourth(self, decision='rejected'):
        inputs, _ = self.grant()
        self.send('acceptance_review', 'fourth')
        self.finish_review('fourth', decision)
        return inputs

    def audit_with(self, engineer_id, reviewer_id, review):
        return self.service.ao_room_acceptance_review_audit(
            self.room, review, ao.digest(self.message.encode()), str(self.database), engineer_id, reviewer_id)

    def first_grant_audit(self):
        first = json.loads((self.journal_dir() / '000001.json').read_bytes())
        return extension._audit(self.directory(), first['inputs']['audit_sha256'])

    def first_grant_owner(self):
        return self.first_grant_audit()['evidence']['native_owner']

    def assert_clean_refusal(self, operation, regex, roles, expected_state='consumed'):
        """Every refusal in this file must, in addition to raising, write nothing: no POST,
        an unchanged lane snapshot, an unchanged AO binding for the named role(s), every
        earlier room file byte-identical, and a `summary()` that still reports the expected
        (pre-operation) state rather than certifying anything corrupted. `expected_state=None`
        means `summary()` itself must still return None (no journal exists yet, as with T8)."""
        if isinstance(roles, str):
            roles = (roles,)
        lane_before = self.lane_snapshot()
        files_before = self.room_files()
        bindings_before = {role: copy.deepcopy(self.state()['bindings'][role]) for role in roles}
        posts = len(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, regex):
            operation()
        self.assertEqual(len(self.fake.posts), posts)
        self.assertEqual(self.lane_snapshot(), lane_before)
        self.assert_preserved(files_before)
        for role in roles:
            self.assertEqual(self.state()['bindings'][role], bindings_before[role])
        summary = extension.summary(self.service, self.state())
        if expected_state is None:
            self.assertIsNone(summary)
        else:
            self.assertIsNotNone(summary)
            self.assertEqual(summary['state'], expected_state)


class NativeOwnerContinuityTests(NativeOwnerContinuityFixture):

    def test_replacement_reviewer_conversation_after_consumed_grant_refuses_then_restores(self):
        """Exact reproduced defect scenario from the review: consume and settle the fourth
        grant, then replace only the reviewer's actual provider conversation (and controller
        generation) in both ownership tables while every AO binding, complete history and
        earlier private record stays untouched. A fifth audit naming the replacement must
        refuse before any write. Auditing again with the *original* reviewer id must now also
        refuse -- the database no longer proves it -- rather than the lane silently treating
        whichever id happens to validate against the room binding as authoritative. Restoring
        the retained native conversation is the only recovery, after which the fifth grant,
        send and acceptance all proceed normally."""
        self.consumed_fourth()
        first_owner = self.first_grant_owner()
        self.replace('reviewer', 'synthetic-replacement-reviewer', 'new-generation')
        self.assert_clean_refusal(
            lambda: self.audit_with('synthetic-native-engineer', 'synthetic-replacement-reviewer', 'fifth'),
            'cannot redefine the retained native reviewer', 'reviewer')
        self.assert_clean_refusal(
            lambda: self.audit_with('synthetic-native-engineer', 'synthetic-native-reviewer', 'fifth'),
            'supplied native session', 'reviewer')
        self.restore('reviewer')
        inputs, grant = self.grant('fifth')
        self.assertTrue(grant['extended'])
        fifth_owner = extension._audit(self.directory(), inputs['audit_sha256'])['evidence']['native_owner']
        self.assertEqual(fifth_owner, first_owner)
        posts = len(self.fake.posts)
        self.send('acceptance_review', 'fifth')
        self.assertEqual(len(self.fake.posts), posts + 1)
        self.finish_review('fifth', 'approved')
        self.assertTrue(self.service.ao_room_accept(self.room, 'fifth')['accepted'])

    def test_replacement_engineer_conversation_after_consumed_grant_refuses_then_restores(self):
        """Symmetric to the reviewer scenario above: the engineer's retained native identity
        is exactly as protected as the reviewer's."""
        self.consumed_fourth()
        first_owner = self.first_grant_owner()
        self.replace('engineer', 'synthetic-replacement-engineer', 'new-generation')
        self.assert_clean_refusal(
            lambda: self.audit_with('synthetic-replacement-engineer', 'synthetic-native-reviewer', 'fifth'),
            'cannot redefine the retained native engineer', 'engineer')
        self.assert_clean_refusal(
            lambda: self.audit_with('synthetic-native-engineer', 'synthetic-native-reviewer', 'fifth'),
            'supplied native session', 'engineer')
        self.restore('engineer')
        inputs, grant = self.grant('fifth')
        self.assertTrue(grant['extended'])
        fifth_owner = extension._audit(self.directory(), inputs['audit_sha256'])['evidence']['native_owner']
        self.assertEqual(fifth_owner, first_owner)
        posts = len(self.fake.posts)
        self.send('acceptance_review', 'fifth')
        self.assertEqual(len(self.fake.posts), posts + 1)
        self.finish_review('fifth', 'approved')
        self.assertTrue(self.service.ao_room_accept(self.room, 'fifth')['accepted'])

    def test_replacement_of_both_roles_refuses_before_any_write(self):
        """Replacing both roles at once must still refuse cleanly before any write; neither
        role's replacement is individually tolerated, let alone both together."""
        self.consumed_fourth()
        self.replace('engineer', 'synthetic-replacement-engineer', 'new-generation')
        self.replace('reviewer', 'synthetic-replacement-reviewer', 'new-generation')
        self.assert_clean_refusal(
            lambda: self.audit_with('synthetic-replacement-engineer', 'synthetic-replacement-reviewer', 'fifth'),
            'cannot redefine the retained native', ('engineer', 'reviewer'))
        self.restore('engineer')
        self.restore('reviewer')
        self.assertTrue(self.audit('fifth')['eligible'])

    def test_same_native_controller_restart_between_and_within_grants_is_compatible(self):
        """A same-native controller restart only ever changes `controller_generation` and
        `activity_state`, never the actual provider conversation id; `_owner` already treats
        those two fields as observations, not replacement-provider authority, so they must
        stay fully compatible: between grants (before the fifth audit), immediately before a
        successful extend, and again immediately before a successful send."""
        self.consumed_fourth()
        first_owner = self.first_grant_owner()
        for role in ('engineer', 'reviewer'):
            self.sql('UPDATE sessions SET controller_generation=?, activity_state=? WHERE id=?',
                     ('generation-2', 'exited', role))
        audit5 = self.audit('fifth')
        self.assertTrue(audit5['eligible'])
        fifth_owner = extension._audit(self.directory(), audit5['audit_sha256'])['evidence']['native_owner']
        self.assertEqual(fifth_owner, first_owner)
        for role in ('engineer', 'reviewer'):
            self.sql('UPDATE sessions SET controller_generation=? WHERE id=?', ('generation-3', role))
        inputs, grant = self.grant('fifth')
        self.assertTrue(grant['extended'])
        for role in ('engineer', 'reviewer'):
            self.sql('UPDATE sessions SET controller_generation=? WHERE id=?', ('generation-4', role))
        posts = len(self.fake.posts)
        self.send('acceptance_review', 'fifth')
        self.assertEqual(len(self.fake.posts), posts + 1)
        self.assertEqual(extension.summary(self.service, self.state())['state'], 'consumed')
        self.finish_review('fifth', 'approved')
        self.assertTrue(self.service.ao_room_accept(self.room, 'fifth')['accepted'])

    def test_legitimate_engineering_correction_between_grants_keeps_retained_owners(self):
        """A legitimate engineering correction between grants changes the candidate the next
        audit freezes, but never the native owners it retains; only a replacement provider
        conversation is refused, not ordinary engineering work in the same scope."""
        self.consumed_fourth('rejected')
        first_owner = self.first_grant_owner()
        first_candidate_sha256 = self.first_grant_audit()['evidence']['checkpoint']['candidate_sha256']
        self.send('correction')
        (self.repo / 'correction.txt').write_text('Legitimate engineering correction.\n')
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        self.service.ao_room_verify(self.room, str(self.repo))
        audit5 = self.audit('fifth')
        self.assertTrue(audit5['eligible'])
        self.assertNotEqual(audit5['candidate_sha256'], first_candidate_sha256)
        fifth_owner = extension._audit(self.directory(), audit5['audit_sha256'])['evidence']['native_owner']
        self.assertEqual(fifth_owner, first_owner)
        self.replace('reviewer', 'synthetic-replacement-reviewer', 'new-generation')
        self.assert_clean_refusal(
            lambda: self.audit_with('synthetic-native-engineer', 'synthetic-replacement-reviewer', 'fifth'),
            'cannot redefine the retained native reviewer', 'reviewer')
        self.restore('reviewer')
        inputs, grant = self.grant('fifth')
        self.assertTrue(grant['extended'])
        self.send('acceptance_review', 'fifth')
        self.finish_review('fifth', 'approved')
        self.assertTrue(self.service.ao_room_accept(self.room, 'fifth')['accepted'])

    def test_replacement_after_two_consumed_grants_refuses_across_the_whole_chain(self):
        """The comparison must reach across the *entire* continuation chain, not merely the
        immediately preceding grant: after two consumed grants (fourth, fifth), a replacement
        reviewer conversation still refuses a sixth audit, and restoring it is still the only
        way forward."""
        self.consumed_fourth()
        first_owner = self.first_grant_owner()
        self.grant('fifth')
        self.send('acceptance_review', 'fifth')
        self.finish_review('fifth', 'rejected')
        self.replace('reviewer', 'synthetic-replacement-reviewer', 'new-generation')
        self.assert_clean_refusal(
            lambda: self.audit_with('synthetic-native-engineer', 'synthetic-replacement-reviewer', 'sixth'),
            'cannot redefine the retained native reviewer', 'reviewer')
        self.restore('reviewer')
        audit6 = self.audit('sixth')
        self.assertTrue(audit6['eligible'])
        sixth_owner = extension._audit(self.directory(), audit6['audit_sha256'])['evidence']['native_owner']
        self.assertEqual(sixth_owner, first_owner)

    def test_contradictory_ownership_in_earlier_grant_audits_refuses_as_chain_defect(self):
        """This exercises the chain-agreement branch without touching bytes, since byte
        tampering is already refused by the manifest and recorded-format checks: only the
        in-memory value `_audit` returns is forged, never anything on disk.

        Deviation from the literal two-grant recipe, and why: with only two total grants,
        "the second grant" and "the latest grant" are the very same journal entry, and
        forging it collides with `_verified`'s own pre-existing, unrelated deep-digest check
        of the latest grant's evidence (`latest['evidence_sha256'] != ao.digest(evidence)`),
        which runs first and raises its own 'audit chain changed' message before this file's
        new `_retained_owners` code is ever reached -- confirmed empirically against this
        fixed runtime while designing this test. Using three consumed grants (fourth, fifth,
        sixth) keeps a genuine middle ("second") grant distinct from the latest (third): the
        latest grant's own recorded audit stays completely intact, so `validate()` succeeds
        exactly as it does in every other test here, and the forged middle grant's evidence
        is what the new chain-agreement comparison in `_retained_owners` catches. This still
        faithfully exercises the exact code path and message the task specifies; only the
        grant count and the final review name (seventh, not sixth) were adapted."""
        self.consumed_fourth()
        self.grant('fifth')
        self.send('acceptance_review', 'fifth')
        self.finish_review('fifth', 'rejected')
        self.grant('sixth')
        self.send('acceptance_review', 'sixth')
        self.finish_review('sixth', 'rejected')
        second_grant_sha = json.loads((self.journal_dir() / '000003.json').read_bytes())['inputs']['audit_sha256']
        real_audit = extension._audit

        def forged(directory, audit_sha256):
            record = real_audit(directory, audit_sha256)
            if audit_sha256 != second_grant_sha:
                return record
            record = copy.deepcopy(record)
            record['evidence']['native_owner']['reviewer']['provider_conversation_id'] = 'forged'
            return record

        with patch.object(extension, '_audit', side_effect=forged):
            self.assert_clean_refusal(lambda: self.audit('seventh'),
                                      'contradictory native ownership', ('engineer', 'reviewer'))
        self.assertTrue(self.audit('seventh')['eligible'])

    def test_first_grant_has_no_chain_and_ordinary_owner_checks_still_apply(self):
        """Before any grant exists there is no chain to defend, and `_retained_owners` returns
        None; the ordinary single-owner check in `_owner` must still refuse a replacement
        conversation on its own, and nothing is stored before the very first grant."""
        self.assert_clean_refusal(
            lambda: self.audit_with('synthetic-native-engineer', 'synthetic-replacement-reviewer', 'fourth'),
            'supplied native session', 'reviewer', expected_state=None)
        self.assertTrue(self.audit('fourth')['eligible'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
