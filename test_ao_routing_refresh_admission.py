"""Pending routing maintenance excludes unrelated mutations; synthetic AO only."""

import copy
import unittest
from unittest.mock import patch

import ao_project_room as ao
import ao_routing
import ao_routing_refresh as refresh
import test_ao_routing_refresh as fixtures


def protected_bytes(case):
    return {
        'room': {str(path.relative_to(case.directory())): path.read_bytes()
                 for path in case.directory().rglob('*') if path.is_file()},
        'runtime': {name: (case.repo / name).read_bytes() for name in ao_routing.FILES},
        'candidate': ao.candidate_snapshot(case.repo),
        'posts': copy.deepcopy(case.fake.posts),
    }


def interrupt_after_first_runtime_file(test, case, retry):
    before = case.state()
    old_files = {name: (case.repo / name).read_bytes() for name in ao_routing.FILES}
    replace = refresh._replace_runtime

    def interrupted(worktree, name, source, target):
        if name == ao_routing.FILES[1]:
            raise OSError('synthetic pending refresh after one runtime replacement')
        return replace(worktree, name, source, target)

    with patch.object(refresh, '_replace_runtime', side_effect=interrupted):
        with test.assertRaisesRegex(OSError, 'after one runtime replacement'):
            retry()
    test.assertEqual(case.state(), before)
    test.assertNotEqual((case.repo / ao_routing.FILES[0]).read_bytes(), old_files[ao_routing.FILES[0]])
    test.assertEqual((case.repo / ao_routing.FILES[1]).read_bytes(), old_files[ao_routing.FILES[1]])
    test.assertEqual(len(refresh._entries(case.directory())), 1)


class RoutingRefreshAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.RoutingRefreshTests('runTest')
        self.addCleanup(self.case.doCleanups)
        self.case.setUp()

    def pending(self):
        interrupt_after_first_runtime_file(self, self.case, self.case.do_refresh)

    def test_spec_registration_and_staged_instructions_preserve_pending_intent_then_retry(self):
        case = self.case
        self.pending()
        before = protected_bytes(case)
        register = lambda: case.service.ao_room_spec_put(case.room, 2, 'A newly authorized synthetic revision.',
            case.gates, 'Actual synthetic approval of this next revision')
        stage = lambda: case.service.ao_room_instruction_stage(case.room, 'pending-amendment',
            'Use the authorized correction.', 'Actual synthetic approval of this instruction')
        for operation in (register, stage):
            with self.assertRaisesRegex(ao.RoomError, 'Unclaimed or missing routing refresh intent'):
                operation()
            self.assertEqual(protected_bytes(case), before)
        result = case.do_refresh()
        self.assertFalse(result['model_dispatch'])
        self.assertEqual(case.state()['routing_refresh']['path'], result['path'])
        self.assertEqual(register()['revision'], 2)  # A committed refresh permits the normal supported mutation.
        self.assertEqual(case.fake.posts, before['posts'])

    def test_sync_refuses_before_observation_writes_but_saved_status_and_retry_remain_available(self):
        case = self.case
        self.pending()
        before = protected_bytes(case)
        with patch.object(case.fake, 'request', side_effect=AssertionError('pending mutation attempted an AO request')):
            status = case.service.ao_room_status(case.room)
            self.assertEqual(status['room_id'], case.room)
            self.assertEqual(status['delegate']['routing']['status'], 'unverified')
            self.assertTrue(any(item['room_id'] == case.room for item in case.service.ao_room_list()['rooms']))
            with self.assertRaisesRegex(ao.RoomError, 'Unclaimed or missing routing refresh intent'):
                case.service.ao_room_sync(case.room)
        self.assertEqual(protected_bytes(case), before)
        case.do_refresh()
        case.service.ao_room_sync(case.room)
        self.assertEqual(case.state()['routing_rules']['source'], 'sync')
        self.assertEqual(case.fake.posts, before['posts'])


class RoutingRefreshGrantAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.AcceptedFourthReviewRefreshTests('runTest')
        self.addCleanup(self.case.doCleanups)
        self.case.setUp()
        self.case.register()
        self.case.fake.snapshots['engineer']['controller'] = 'stopped'

    def retry(self):
        case = self.case
        return refresh.refresh(case.service, case.room, 'pending-before-fourth-grant', str(case.database),
            'synthetic-native-owner', 'Actual synthetic authorization for this retained routing update.',
            'Synthetic interruption before a fourth-review grant.')

    def test_pending_refresh_blocks_new_grant_audit_without_publishing_or_stranding_retry(self):
        case = self.case
        interrupt_after_first_runtime_file(self, case, self.retry)
        before = protected_bytes(case)
        with self.assertRaisesRegex(ao.RoomError, 'Unclaimed or missing routing refresh intent'):
            case.audit()
        self.assertEqual(protected_bytes(case), before)
        self.retry()
        self.assertEqual(case.grant()['remaining_spec_reviews'], 1)
        self.assertEqual(case.fake.posts, before['posts'])

    def test_saved_valid_grant_audit_cannot_commit_during_pending_refresh(self):
        case = self.case
        inputs = case.grant_inputs()
        interrupt_after_first_runtime_file(self, case, self.retry)
        before = protected_bytes(case)
        with self.assertRaisesRegex(ao.RoomError, 'Unclaimed or missing routing refresh intent'):
            case.service.ao_room_spec_review_extend(**inputs)
        self.assertEqual(protected_bytes(case), before)
        self.assertNotIn('spec_review_extension', case.state())
        self.retry()
        self.assertEqual(case.grant()['remaining_spec_reviews'], 1)
        self.assertEqual(case.fake.posts, before['posts'])


if __name__ == '__main__':
    unittest.main()
