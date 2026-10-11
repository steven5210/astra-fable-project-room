"""Production-entry regressions for acceptance finding P2 (the two non-routing items).

``ao_review_extension``'s pre-acceptance lane binds the exact immutable candidate-at-completion
``implementation.candidate_snapshot`` recorded when the scope-change engineering request completed
(``ao_workflow.capture_engineering`` -> ``completion_candidate``, saved under
``completion-candidates/<request>.json``): ``_scope_proposal`` refuses once the live worktree no
longer matches that saved snapshot byte for byte, never a plain dirty/clean flag. This module proves
that an *already-dirty* captured snapshot is itself fully eligible for audit, grant and the fourth
review send for as long as the worktree keeps matching it exactly, and that drifting away from it --
by reverting, by committing, or by changing its dirty content again -- refuses with the lane's one
candidate-drift message, exactly like every other drift proof in the sibling module.

``_inspect`` separately refuses an audit whose current spec is not exactly the one most-recently
reviewed revision plus one -- even when the caller's supplied revision and digest exactly match the
room's own current spec identity. This module also proves that a *genuinely registered* skipped
charter revision (registered for real, through ``ao_room_spec_put``, but never reviewed) still
refuses that same audit, both when cited directly and when cited by its own now-superseded identity.

Both proofs go through the same production Service methods the sibling module uses
(``ao_room_preacceptance_review_audit``, ``ao_room_preacceptance_review_extend``, ``ao_room_send``,
``ao_room_spec_put``); this module imports ``PreAcceptanceFixture`` rather than copying it. See
``test_ao_preacceptance_review_extension.py`` for the fixture itself and the lane's full existing
coverage.
"""
import subprocess
import unittest

import implementation
from test_ao_preacceptance_review_extension import PreAcceptanceFixture

DRIFT_MESSAGE = 'unaccepted candidate changed since the scope-change proposal completed'
SKIPPED_REVISION_MESSAGE = 'Register the exact next charter revision before its review-extension audit'


class PreAcceptanceDirtyCandidateEligibilityTests(PreAcceptanceFixture):
    """Finding P2 (item a): eligibility with an already-dirty candidate captured at completion.

    ``implementation.candidate_snapshot`` (implementation.py:444-479) binds every tracked and
    untracked committable path's actual content -- a per-file ``sha256`` over its real bytes for a
    regular file, or its symlink target, plus file mode, deletions, the full git index (path, mode,
    blob, stage) and the current HEAD commit -- then one further ``sha256`` over that whole payload
    (implementation.py:467, :477-479). It is not a boolean and is computed the same way regardless of
    whether the worktree is clean or dirty, so a dirty worktree captured at engineering completion is
    exactly as eligible as a clean one for as long as later reads reproduce the identical payload.
    """

    def dirty(self, tracked='dirty-tracked\n', untracked='dirty-untracked\n'):
        """Make the fixture's own worktree dirty: one modified tracked file, one new untracked file."""
        (self.repo / 'feature.txt').write_text(tracked)
        (self.repo / 'untracked.txt').write_text(untracked)

    def commit_dirty(self, message='commit the dirty edits'):
        subprocess.run(['git', '-C', str(self.repo), 'add', '-A'], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(self.repo), 'commit', '-m', message], check=True, capture_output=True)

    def test_dirty_candidate_at_completion_stays_eligible_through_audit_grant_and_fourth_send(self):
        self.dirty()
        self.propose_scope_change()
        expected = implementation.candidate_snapshot(self.repo)['sha256']
        # The captured candidate-at-completion is itself dirty, not a clean baseline snapshot.
        self.assertEqual(self.candidate_sha256(), expected)
        audited = self.audit()
        self.assertTrue(audited['eligible'])
        self.assertEqual(audited['candidate_sha256'], expected)
        # The live worktree still matches that exact dirty snapshot; nothing reverted it meanwhile.
        self.assertEqual(implementation.candidate_snapshot(self.repo)['sha256'], expected)
        granted = self.grant()
        self.assertEqual(granted['remaining_spec_reviews'], 1)
        sent = self.send('spec_review', 'fourth')
        self.assertEqual(sent['state'], 'submitted')
        self.assertIn('fourth', self.state()['requests'])

    def test_reverting_the_dirty_edits_after_capture_refuses_the_audit(self):
        self.dirty()
        self.propose_scope_change()
        self.register()  # Prime self.target_spec so the audit-without-mutation check below is not
                          # attributed to this unrelated, one-time-per-test next-charter registration.
        (self.repo / 'feature.txt').write_text('start\n')
        (self.repo / 'untracked.txt').unlink()
        self.assert_refuses_without_mutation(DRIFT_MESSAGE, self.audit)

    def test_reverting_the_dirty_edits_after_a_grant_refuses_the_fourth_send(self):
        self.dirty()
        self.propose_scope_change()
        self.grant()
        (self.repo / 'feature.txt').write_text('start\n')
        (self.repo / 'untracked.txt').unlink()
        self.assert_refuses_without_mutation(DRIFT_MESSAGE, lambda: self.send('spec_review', 'fourth'))
        self.assertNotIn('fourth', self.state()['requests'])

    def test_committing_the_dirty_edits_after_capture_refuses_the_audit(self):
        self.dirty()
        self.propose_scope_change()
        self.register()  # Prime self.target_spec; see the revert test above for why.
        self.commit_dirty()
        self.assert_refuses_without_mutation(DRIFT_MESSAGE, self.audit)

    def test_different_dirty_tracked_content_after_capture_refuses_the_audit(self):
        """candidate_snapshot binds the tracked file's actual bytes, not only head or a dirty flag:
        the head commit and the git index are unchanged, and the worktree is dirty either way; only
        the uncommitted content itself differs from what completion captured."""
        self.dirty()
        self.propose_scope_change()
        self.register()  # Prime self.target_spec; see the revert test above for why.
        (self.repo / 'feature.txt').write_text('dirty-tracked-different\n')
        self.assert_refuses_without_mutation(DRIFT_MESSAGE, self.audit)

    def test_different_untracked_file_after_capture_refuses_the_audit(self):
        """Same proof as above, through an additional untracked file instead of changed tracked bytes:
        head and the tracked index entries are unchanged; only the untracked set differs."""
        self.dirty()
        self.propose_scope_change()
        self.register()  # Prime self.target_spec; see the revert test above for why.
        (self.repo / 'another-untracked.txt').write_text('more-dirty\n')
        self.assert_refuses_without_mutation(DRIFT_MESSAGE, self.audit)


class PreAcceptanceSkippedRevisionTests(PreAcceptanceFixture):
    """Finding P2 (item b): a genuinely registered skipped charter revision.

    ``ao_room_spec_put`` only requires a strictly newer revision than the room's current one
    (ao_project_room.py:555-556); it does not require every intermediate revision to have been
    reviewed first, so revision 3 can be registered directly on top of a real, saved, but never
    reviewed revision 2. ``_inspect`` (ao_review_extension.py:548-552) then refuses the audit either
    way: revision 3 exactly matches the room's current spec but is not the agreed revision (1) plus
    one, and revision 2 -- though genuinely registered and cited with its own real digest -- no
    longer identifies the room's current spec at all, since revision 3 superseded it.
    """

    def test_skipped_revision_with_matching_identity_refuses_the_audit(self):
        self.propose_scope_change()
        spec2 = self.register()
        spec3 = self.service.ao_room_spec_put(
            self.room, 3, 'A further charter registered directly, skipping any review at revision 2.',
            self.gates, 'Astra approves this further scope decision')
        self.assertEqual(spec3['revision'], 3)
        self.assertEqual(self.service.spec(self.directory(), self.state())['revision'], 3)

        def audit_at(revision, spec):
            return self.service.ao_room_preacceptance_review_audit(
                room_id=self.room, spec_revision=revision, spec_sha256=spec['sha256'],
                scope_request_id='implementation', candidate_sha256=self.candidate_sha256(),
                native_session_id=self.NATIVE, native_owner_database=str(self.database))

        # The skip itself: revision 3 is the room's genuine current spec, exactly matching the
        # caller's supplied revision and digest, but it is not revision 1 (the agreed reviews) plus one.
        self.assert_refuses_without_mutation(SKIPPED_REVISION_MESSAGE, lambda: audit_at(3, spec3))
        # The superseded revision: revision 2 is also genuinely registered and cited with its own
        # real digest, but revision 3 is now current, so this is an identity mismatch against the
        # room's actual current spec rather than a not-next-revision mismatch; _inspect's single
        # combined check still refuses with the exact same message either way.
        self.assert_refuses_without_mutation(SKIPPED_REVISION_MESSAGE, lambda: audit_at(2, spec2))


if __name__ == '__main__':
    unittest.main()
