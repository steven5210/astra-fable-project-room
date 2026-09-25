"""Receipt template and strict receipt validation tests (pure, all platforms)."""

import json
import unittest
from unittest import mock

import changeset_receipts
from changeset_core import ChangesetError, canonical, digest_object, sha256
from changeset_receipts import RECEIPT_TOP, staged_set_digest, template_for, validate_receipt

SHA_A = 'a' * 64
SHA_T = 'c' * 64
COND_A = 'SENTINEL-COND-A'
COND_B = 'SENTINEL-COND-B'
COND_POST = 'SENTINEL-COND-POST'
INPUT_A = 'in-a'


def plan_with(conditions=None):
    if conditions is None:
        conditions = [
            {'id': COND_A, 'phase': 'pre_apply', 'text': 'check a', 'description': 'desc a',
             'inputs': [{'id': INPUT_A, 'root': 'input', 'path': 'pins/a.txt', 'bytes': 3,
                         'sha256': SHA_A}],
             'why_not_before': None, 'failure_handling': None},
            {'id': COND_B, 'phase': 'pre_apply', 'text': 'check b', 'description': 'desc b',
             'inputs': [], 'why_not_before': None, 'failure_handling': None},
            {'id': COND_POST, 'phase': 'post_apply', 'text': 'check c', 'description': 'desc c',
             'inputs': [], 'why_not_before': 'needs applied bytes',
             'failure_handling': 'operator review'},
        ]
    plan = {'plan_version': 1, 'batch_id': 'batch', 'execution': 'sequential-v1',
            'workspace': {'path': '/ws', 'device': 1, 'inode': 2, 'mode': 0o755},
            'input_root': {'path': '/in', 'device': 1, 'inode': 3},
            'targets': [{'target': 'report.json',
                         'proposed': {'bytes': 12, 'sha256': SHA_T}}],
            'conditions': conditions, 'pinned_inputs': []}
    plan['plan_sha256'] = digest_object(plan, 'plan_sha256')
    return plan


def receipt(plan=None, results=None, drop=(), add=(), change=None):
    plan = plan if plan is not None else plan_with()
    template = template_for(plan)
    results = results or {}
    data = dict(template)
    data['conditions'] = []
    for cond in template['conditions']:
        if cond['id'] in drop:
            continue
        entry = dict(cond)
        entry['result'] = results.get(entry['id'], 'pass')
        data['conditions'].append(entry)
    for extra in add:
        data['conditions'].append(dict(extra))
    data['input_digests'] = [dict(item) for item in template['input_digests']]
    if change is not None:
        change(data)
    return data, plan


def check(data, plan):
    return validate_receipt(canonical(data).encode('utf-8'), plan)


class ReceiptTests(unittest.TestCase):
    def assertRefused(self, code, data, plan):
        with self.assertRaises(ChangesetError) as caught:
            check(data, plan)
        self.assertEqual(caught.exception.code, code)

    def test_template_is_receipt_shaped_and_pre_only(self):
        plan = plan_with()
        template = template_for(plan)
        self.assertEqual(set(template), RECEIPT_TOP)
        self.assertEqual(template['receipt_version'], 1)
        self.assertNotIn('receipt_template_version', template)
        self.assertEqual(template['plan_sha256'], plan['plan_sha256'])
        self.assertEqual(template['staged_set_sha256'], staged_set_digest(plan))
        self.assertEqual([cond['id'] for cond in template['conditions']], [COND_A, COND_B])
        self.assertEqual({cond['result'] for cond in template['conditions']}, {'unknown'})
        self.assertEqual(template['input_digests'],
                         [{'condition_id': COND_A, 'input_id': INPUT_A, 'sha256': SHA_A}])
        self.assertNotIn(COND_POST, json.dumps(template['conditions']))

    def test_unchanged_nonempty_template_cannot_pass(self):
        plan = plan_with()
        self.assertRefused('receipt_not_passing', template_for(plan), plan)

    def test_copy_fill_pass_is_a_valid_receipt(self):
        plan = plan_with()
        template = template_for(plan)
        filled = dict(template)
        filled['conditions'] = [dict(cond, result='pass') for cond in template['conditions']]
        result = check(filled, plan)
        expected = sorted([sha256(COND_A.encode('utf-8')), sha256(COND_B.encode('utf-8'))])
        self.assertEqual(result['conditions'], expected)
        blob = json.dumps(result)
        self.assertNotIn(COND_A, blob)
        self.assertNotIn(COND_B, blob)

    def test_no_condition_declaration_is_copy_fill_complete(self):
        plan = plan_with(conditions=[])
        template = template_for(plan)
        self.assertEqual(template['conditions'], [])
        self.assertEqual(template['input_digests'], [])
        self.assertEqual(check(template, plan),
                         {'sha256': sha256(canonical(template).encode('utf-8')),
                          'conditions': []})
        data, plan2 = receipt(plan_with(conditions=[]))
        data['conditions'].append({'id': COND_A, 'phase': 'pre_apply', 'result': 'pass'})
        self.assertRefused('receipt_condition_mismatch', data, plan2)

    def test_missing_extra_duplicate_and_post_conditions_refuse(self):
        plan = plan_with()
        data, _ = receipt(plan, drop=(COND_B,))
        self.assertRefused('receipt_condition_mismatch', data, plan)
        data, _ = receipt(plan, add=[{'id': 'extra', 'phase': 'pre_apply', 'result': 'pass'}])
        self.assertRefused('receipt_condition_mismatch', data, plan)
        data, _ = receipt(plan, add=[{'id': COND_A, 'phase': 'pre_apply', 'result': 'pass'}])
        self.assertRefused('duplicate_receipt_condition', data, plan)
        data, _ = receipt(plan, add=[{'id': COND_POST, 'phase': 'post_apply', 'result': 'pass'}])
        self.assertRefused('receipt_condition_mismatch', data, plan)
        data, _ = receipt(plan)
        data['conditions'][0]['phase'] = 'post_apply'
        self.assertRefused('receipt_condition_mismatch', data, plan)

    def test_failing_unknown_and_edited_results_refuse(self):
        plan = plan_with()
        self.assertRefused('receipt_not_passing', receipt(plan, results={COND_A: 'fail'})[0], plan)
        self.assertRefused('receipt_not_passing',
                           receipt(plan, results={COND_B: 'unknown'})[0], plan)
        self.assertRefused('receipt_not_passing',
                           receipt(plan, results={COND_A: 'PASS'})[0], plan)

    def test_stale_plan_staged_and_input_digests_refuse(self):
        plan = plan_with()
        data, _ = receipt(plan)
        data['plan_sha256'] = 'd' * 64
        self.assertRefused('stale_plan_digest', data, plan)
        data, _ = receipt(plan)
        data['staged_set_sha256'] = 'd' * 64
        self.assertRefused('stale_staged_digest', data, plan)
        data, _ = receipt(plan)
        data['input_digests'][0]['sha256'] = 'd' * 64
        self.assertRefused('stale_input_digest', data, plan)

    def test_missing_extra_and_duplicate_input_entries_refuse(self):
        plan = plan_with()
        data, _ = receipt(plan, change=lambda d: d['input_digests'].clear())
        self.assertRefused('receipt_input_mismatch', data, plan)
        data, _ = receipt(plan, change=lambda d: d['input_digests'].append(
            {'condition_id': COND_B, 'input_id': 'ghost', 'sha256': 'd' * 64}))
        self.assertRefused('receipt_input_mismatch', data, plan)
        data, _ = receipt(plan, change=lambda d: d['input_digests'].append(
            dict(d['input_digests'][0])))
        self.assertRefused('duplicate_receipt_input', data, plan)

    def test_malformed_receipts_refuse(self):
        plan = plan_with()
        data, _ = receipt(plan)
        data['extra'] = True
        self.assertRefused('invalid_receipt', data, plan)
        data, _ = receipt(plan)
        data['receipt_version'] = 2
        self.assertRefused('invalid_receipt', data, plan)
        data, _ = receipt(plan)
        data['input_digests'][0]['sha256'] = 'zz'
        self.assertRefused('invalid_receipt', data, plan)
        data, _ = receipt(plan)
        data['conditions'] = {'not': 'a list'}
        self.assertRefused('invalid_receipt', data, plan)
        data, _ = receipt(plan)
        data['conditions'][0].pop('result')
        self.assertRefused('invalid_receipt', data, plan)
        data, _ = receipt(plan)
        data['receipt_version'] = 1.0
        with self.assertRaises(ChangesetError) as caught:
            validate_receipt(json.dumps(data).encode('utf-8'), plan)
        self.assertEqual(caught.exception.code, 'invalid_receipt')
        with self.assertRaises(ChangesetError) as caught:
            validate_receipt(b'{"receipt_version": 1, "receipt_version": 1}', plan)
        self.assertEqual(caught.exception.code, 'duplicate_json_key')

    def test_legacy_template_version_cannot_produce_a_valid_receipt(self):
        plan = plan_with()
        legacy = dict(template_for(plan))
        legacy.pop('receipt_version')
        legacy['receipt_template_version'] = 1
        legacy['conditions'] = [dict(cond, result='pass') for cond in legacy['conditions']]
        with self.assertRaises(ChangesetError) as caught:
            check(legacy, plan)
        self.assertEqual(caught.exception.code, 'invalid_receipt')

    def test_receipt_resource_bound_is_enforced(self):
        plan = plan_with()
        data, _ = receipt(plan)
        blob = json.dumps(data).encode('utf-8')
        with mock.patch.object(changeset_receipts, 'MAX_FILE_BYTES', 8):
            with self.assertRaises(ChangesetError) as caught:
                validate_receipt(blob, plan)
        self.assertEqual(caught.exception.code, 'input_too_large')


if __name__ == '__main__':
    unittest.main()
