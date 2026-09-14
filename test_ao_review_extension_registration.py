"""Keep an unused exact-charter grant usable across specification API calls."""
import copy
import unittest
from unittest.mock import patch

import ao_project_room as ao
import ao_review_extension as extension
from test_ao_review_extension import ReviewExtensionFixture


class ReviewExtensionRegistrationTests(ReviewExtensionFixture):
    def files(self):
        return {str(path.relative_to(self.directory())): path.read_bytes()
                for path in self.directory().rglob('*') if path.is_file()}

    def test_new_revision_refuses_before_write_and_original_fourth_review_still_completes(self):
        grant = self.grant()
        before, posts = self.files(), copy.deepcopy(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, 'unused fourth-review grant.*different spec was not saved'):
            self.service.ao_room_spec_put(self.room, 3, 'A different charter needs a separate scope decision.',
                                          self.gates, 'Actual approval to propose this new revision')
        self.assertEqual(self.files(), before)
        self.assertEqual(self.fake.posts, posts)
        self.assertEqual(self.service.ao_room_status(self.room)['spec_review_extension']['remaining_spec_reviews'], 1)
        self.send('spec_review', 'fourth')
        self.finish_fourth()
        current = self.service.ao_room_status(self.room)['spec_review_extension']
        self.assertEqual(current['remaining_spec_reviews'], 0)
        self.assertEqual(current['receipt_sha256'], grant['receipt_sha256'])

    def test_identical_current_spec_is_a_read_without_native_observation_or_mutation(self):
        self.grant()
        current, before = self.target_spec, self.files()
        with (patch.object(self.service, 'save', side_effect=AssertionError('Identical registration must not save')),
              patch.object(self.service, 'client', side_effect=AssertionError('Identical registration must not contact AO'))):
            result = self.service.ao_room_spec_put(self.room, current['revision'], current['content'],
                                                    current['gates'], current['approval'])
        self.assertEqual(result, current)
        self.assertEqual(self.files(), before)

    def test_pending_grant_already_blocks_new_spec_without_breaking_identical_reconcile(self):
        inputs = self.grant_inputs()
        with patch.object(self.service, 'save', side_effect=OSError('Synthetic grant projection crash')):
            with self.assertRaises(OSError):
                self.service.ao_room_spec_review_extend(**inputs)
        self.assertEqual(len(extension._pending(self.directory())), 1)
        before = self.files()
        with self.assertRaisesRegex(ao.RoomError, 'uncommitted review-extension receipt'):
            self.service.ao_room_spec_put(self.room, 3, 'A different charter.', self.gates, 'Actual new approval')
        self.assertEqual(self.files(), before)
        reconciled = self.service.ao_room_spec_review_extend(**inputs)
        self.assertEqual(reconciled['remaining_spec_reviews'], 1)
        self.assertEqual(len(extension._pending(self.directory())), 1)

    def test_completed_grant_does_not_change_existing_registration_or_fifth_review_limit(self):
        self.grant()
        self.send('spec_review', 'fourth')
        self.finish_fourth()
        grant = copy.deepcopy(self.state()['spec_review_extension'])
        self.service.ao_room_spec_put(self.room, 3, 'Explicit later proposed revision.', self.gates, 'Actual later approval')
        self.assertEqual(self.state()['spec_review_extension'], grant)
        self.assertEqual(self.service.ao_room_status(self.room)['spec_review_extension']['remaining_spec_reviews'], 0)
        with self.assertRaisesRegex(ao.RoomError, 'fourth.*exhausted'):
            self.send('spec_review', 'fifth')
        self.assertNotIn('fifth', self.state()['requests'])


if __name__ == '__main__':
    unittest.main()
