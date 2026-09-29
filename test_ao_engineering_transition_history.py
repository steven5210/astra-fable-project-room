"""Actual-path history proof renewal regression for the engineering-model transition core.

The synthetic pointer-head unit in test_ao_engineering_transition.py stays a narrow unit; this file
composes the supplied HistoryReconciliationTests fixture and exercises the real audit/reconcile API.
Nothing here claims to have been run.
"""

import unittest

import ao_engineering_transition as et
import ao_history_reconciliation as reconciliation
from room import RoomError


class HistoryProofRealRenewalTests(unittest.TestCase):
    def test_real_audit_reconcile_invalidates_renews_and_changed_input_refuses(self):
        from test_ao_history_reconciliation import HistoryReconciliationTests
        helper = HistoryReconciliationTests('runTest')
        self.addCleanup(helper.doCleanups)
        helper.setUp()
        first = helper.audit()
        self.assertTrue(first['resume_eligible'], first)
        request = helper.current()
        old_proof = request[reconciliation.PROOF]
        self.assertIsNotNone(old_proof)
        et._require_complete_history_proof(helper.directory(), request)
        old_receipt = helper.receipt_path.read_bytes()
        proof_path = helper.directory() / 'history-reconciliations' / 'implementation' / (old_proof + '.json')
        old_proof_bytes = proof_path.read_bytes()
        original_text = helper.snapshot['messages'][-1]['text']
        helper.snapshot['messages'][-1]['text'] = 'Changed before renewal'
        helper.service.ao_room_sync(helper.room)
        request = helper.current()
        head = request[reconciliation.INVALIDATION]
        self.assertIsNotNone(head)
        head_path = helper.directory() / 'history-reconciliation-invalidations' / 'implementation' / (head + '.json')
        head_bytes = head_path.read_bytes()
        with self.assertRaisesRegex(RoomError, 'complete-history reconciliation proof'):
            et._require_complete_history_proof(helper.directory(), request)
        helper.snapshot['messages'][-1]['text'] = original_text
        renewed = helper.audit()
        self.assertTrue(renewed['resume_eligible'], renewed)
        request = helper.current()
        new_proof = request[reconciliation.PROOF]
        self.assertIsNotNone(new_proof)
        self.assertNotEqual(new_proof, old_proof)
        self.assertIsNotNone(request[reconciliation.INVALIDATION])
        et._require_complete_history_proof(helper.directory(), request)
        self.assertEqual(helper.receipt_path.read_bytes(), old_receipt)
        self.assertEqual(proof_path.read_bytes(), old_proof_bytes)
        self.assertEqual(head_path.read_bytes(), head_bytes)
        helper.events[-1].update(error='invalid_request', apiErrorStatus=400)
        helper.write_native()
        changed = helper.audit()
        self.assertFalse(changed['resume_eligible'])
        with self.assertRaisesRegex(RoomError, 'complete-history reconciliation proof'):
            et._require_complete_history_proof(helper.directory(), helper.current())


if __name__ == '__main__':
    unittest.main()
