"""Routing maintenance cannot reuse, renew or replace an extra charter review."""
import copy
import json

import ao_project_room as ao
import ao_review_extension as extension
from test_ao_review_extension import ReviewExtensionFixture


class RoutingRefreshGrantTests(ReviewExtensionFixture):
    def owner(self):
        return extension.ao_native_identity.read_owner(str(self.database), 'engineer')

    def guard(self):
        return extension.guard_routing_refresh(self.service, self.state(), self.owner())

    def test_unused_and_inflight_fourth_review_stay_blocked(self):
        self.grant()
        with self.assertRaisesRegex(ao.RoomError, 'unused fourth-review'):
            self.guard()
        self.send('spec_review', 'fourth')
        with self.assertRaisesRegex(ao.RoomError, 'completed accepted'):
            self.guard()

    def test_completed_accepted_fourth_keeps_all_evidence_and_attempts(self):
        self.grant(); self.send('spec_review', 'fourth'); self.finish_fourth()
        before = copy.deepcopy(self.state()); posts = len(self.fake.posts)
        result = self.guard()
        self.assertEqual(result['consumed_by'], 'fourth')
        self.assertEqual(result['agreement']['request_id'], 'fourth')
        self.assertEqual(result['grant_receipt_sha256'], before['spec_review_extension']['receipt_sha256'])
        self.assertEqual(self.state(), before)
        self.assertEqual(len(self.fake.posts), posts)
        with self.assertRaises(ao.RoomError):
            self.send('spec_review', 'fifth')

    def test_rejected_fourth_does_not_qualify(self):
        self.grant(); self.send('spec_review', 'fourth')
        self.fake.finish('engineer', json.dumps({'interpretation': 'A conflict remains.',
            'findings': ['BLOCKER: Need a product decision'], 'decision': 'changes_required',
            'spec_revision': 2, 'spec_sha256': self.target_spec['sha256']}))
        self.service.ao_room_sync(self.room)
        with self.assertRaisesRegex(ao.RoomError, 'rejected'):
            self.guard()

    def test_foreign_owner_refused_even_after_acceptance(self):
        self.grant(); self.send('spec_review', 'fourth'); self.finish_fourth()
        owner = self.owner(); owner['provider_conversation_id'] = 'replacement'
        with self.assertRaisesRegex(ao.RoomError, 'same native owner'):
            extension.guard_routing_refresh(self.service, self.state(), owner)

    def test_later_typed_hold_invalidates_accepted_fourth_for_maintenance(self):
        self.grant(); self.send('spec_review', 'fourth'); self.finish_fourth()
        self.fake.snapshots['engineer']['turns'][-1]['error'] = {'type': 'quota_limit'}
        self.service.ao_room_sync(self.room)
        before = copy.deepcopy(self.state()); posts = len(self.fake.posts)
        self.assertTrue(before['requests']['fourth']['semantic_status']['hold'])
        with self.assertRaisesRegex(ao.RoomError, 'semantic hold'):
            self.guard()
        self.assertEqual(self.state(), before)
        self.assertEqual(len(self.fake.posts), posts)

    def test_changed_exact_spec_cannot_reuse_prior_fourth_agreement(self):
        self.grant(); self.send('spec_review', 'fourth'); self.finish_fourth()
        self.service.ao_room_spec_put(self.room, 3, 'A different authorized charter.',
                                     self.gates, 'Actual approval of this different charter')
        before = copy.deepcopy(self.state()); posts = len(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, 'current exact spec'):
            self.guard()
        self.assertEqual(self.state(), before)
        self.assertEqual(len(self.fake.posts), posts)
