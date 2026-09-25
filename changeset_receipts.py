"""Private receipt template and strict receipt validation.

A template is receipt-shaped (receipt_version 1, results unknown) so that the
operator copies it to a separate file and fills results: copy+fill yields a
valid receipt, including for the explicitly declared no-condition plan.
"""

from changeset_core import *

RECEIPT_VERSION = 1
RECEIPT_TOP = {'receipt_version', 'plan_sha256', 'staged_set_sha256', 'input_digests', 'conditions'}
INPUT_ENTRY_KEYS = {'condition_id', 'input_id', 'sha256'}
PLAN_INPUT_KEYS = {'id', 'root', 'path', 'bytes', 'sha256'}


def _hex64(value):
    if not isinstance(value, str) or len(value) != 64:
        return False
    for ch in value:
        if ch not in '0123456789abcdef':
            return False
    return True


def _plan_sha(plan):
    if not isinstance(plan, dict):
        refuse('invalid_plan')
    value = plan.get('plan_sha256')
    if not _hex64(value):
        refuse('invalid_plan')
    return value


def staged_set_digest(plan):
    if not isinstance(plan, dict) or not isinstance(plan.get('targets'), list):
        refuse('invalid_plan')
    items = []
    for target in plan['targets']:
        if not isinstance(target, dict) or not isinstance(target.get('target'), str):
            refuse('invalid_plan')
        proposed = target.get('proposed')
        if not isinstance(proposed, dict) or type(proposed.get('bytes')) is not int \
                or not _hex64(proposed.get('sha256')):
            refuse('invalid_plan')
        items.append({'target': target['target'], 'sha256': proposed['sha256'],
                      'bytes': proposed['bytes']})
    return sha256(canonical({'items': items}).encode('utf-8'))


def _pre_conditions(plan):
    if not isinstance(plan, dict) or not isinstance(plan.get('conditions'), list):
        refuse('invalid_plan')
    pre = []
    for cond in plan['conditions']:
        if not isinstance(cond, dict):
            refuse('invalid_plan')
        if cond.get('phase') != 'pre_apply':
            continue
        cond_id = cond.get('id')
        if not isinstance(cond_id, str) or not cond_id:
            refuse('invalid_plan')
        inputs = cond.get('inputs')
        if not isinstance(inputs, list):
            refuse('invalid_plan')
        entries = []
        for item in inputs:
            if not isinstance(item, dict) or set(item) != PLAN_INPUT_KEYS:
                refuse('invalid_plan')
            input_id = item['id']
            if not isinstance(input_id, str) or not input_id or not _hex64(item['sha256']):
                refuse('invalid_plan')
            entries.append((input_id, item['sha256']))
        pre.append((cond_id, entries))
    return pre


def template_for(plan):
    plan_sha = _plan_sha(plan)
    inputs = []
    conditions = []
    for cond_id, entries in _pre_conditions(plan):
        for input_id, shaval in entries:
            inputs.append({'condition_id': cond_id, 'input_id': input_id, 'sha256': shaval})
        conditions.append({'id': cond_id, 'phase': 'pre_apply', 'result': 'unknown', 'evidence': ''})
    return {'receipt_version': RECEIPT_VERSION, 'plan_sha256': plan_sha,
            'staged_set_sha256': staged_set_digest(plan),
            'input_digests': inputs, 'conditions': conditions}


def validate_receipt(receipt_bytes, plan):
    plan_sha = _plan_sha(plan)
    expected_staged = staged_set_digest(plan)
    pre = _pre_conditions(plan)
    required_ids = [cond_id for cond_id, _ in pre]
    if len(set(required_ids)) != len(required_ids):
        refuse('invalid_plan')
    expected_inputs = []
    for cond_id, entries in pre:
        for input_id, shaval in entries:
            expected_inputs.append((cond_id, input_id, shaval))
    expected_keys = [(cond_id, input_id) for cond_id, input_id, _ in expected_inputs]
    if len(set(expected_keys)) != len(expected_keys):
        refuse('invalid_plan')
    expected = {(cond_id, input_id): shaval for cond_id, input_id, shaval in expected_inputs}
    receipt = strict_loads(receipt_bytes, limit=MAX_FILE_BYTES)
    if not isinstance(receipt, dict) or set(receipt) != RECEIPT_TOP:
        refuse('invalid_receipt')
    if type(receipt['receipt_version']) is not int or receipt['receipt_version'] != RECEIPT_VERSION:
        refuse('invalid_receipt')
    if receipt['plan_sha256'] != plan_sha:
        refuse('stale_plan_digest')
    if receipt['staged_set_sha256'] != expected_staged:
        refuse('stale_staged_digest')
    entries = receipt['input_digests']
    if not isinstance(entries, list):
        refuse('invalid_receipt')
    seen_inputs = set()
    for item in entries:
        if not isinstance(item, dict) or set(item) != INPUT_ENTRY_KEYS:
            refuse('invalid_receipt')
        if not isinstance(item['condition_id'], str) or not isinstance(item['input_id'], str):
            refuse('invalid_receipt')
        if not _hex64(item['sha256']):
            refuse('invalid_receipt')
        key = (item['condition_id'], item['input_id'])
        if key in seen_inputs:
            refuse('duplicate_receipt_input')
        seen_inputs.add(key)
        if key not in expected:
            refuse('receipt_input_mismatch')
        if expected[key] != item['sha256']:
            refuse('stale_input_digest')
    if seen_inputs != set(expected):
        refuse('receipt_input_mismatch')
    conditions = receipt['conditions']
    if not isinstance(conditions, list):
        refuse('invalid_receipt')
    required = set(required_ids)
    seen_conditions = set()
    for item in conditions:
        if not isinstance(item, dict) or not {'id', 'phase', 'result'} <= set(item) \
                or set(item) - {'id', 'phase', 'result', 'evidence'}:
            refuse('invalid_receipt')
        cond_id = item['id']
        if not isinstance(cond_id, str) or not cond_id:
            refuse('invalid_receipt')
        if cond_id in seen_conditions:
            refuse('duplicate_receipt_condition')
        seen_conditions.add(cond_id)
        if cond_id not in required or item['phase'] != 'pre_apply':
            refuse('receipt_condition_mismatch')
        if item['result'] != 'pass':
            refuse('receipt_not_passing')
        if 'evidence' in item and not isinstance(item['evidence'], str):
            refuse('invalid_receipt')
    if seen_conditions != required:
        refuse('receipt_condition_mismatch')
    # Public identity only: opaque condition IDs leave this function as digests
    # (the journal echoes this list into its public result).
    return {'sha256': sha256(receipt_bytes),
            'conditions': sorted(id_digest(cond_id) for cond_id in required)}
