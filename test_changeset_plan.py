"""End-to-end plan API tests on synthetic owned temp roots.

The plan API needs POSIX descriptors and platform capabilities; when the
bounded refusal says the host cannot support it, the test skips with that
reason.  Pure core/receipt tests still run on every platform.  Expected bytes
are literals, never produced by the production renderer.
"""

import json
import copy
import os
import shutil
import stat
import tempfile
import unittest
from unittest import mock

from changeset_core import ChangesetError, canonical, digest_object, sha256, strict_loads
from changeset_receipts import staged_set_digest, validate_receipt

SOURCE = b'{\n  "note": "hello old world"\n}\n'
EXPECTED = b'{\n  "note": "hello new world"\n}\n'
DRAFT = (
    b'{\n'
    b'  "changeset_format": "json-ops-v1",\n'
    b'  "document": "report.json",\n'
    b'  "request_id": "req-1",\n'
    b'  "operations": [\n'
    b'    {\n'
    b'      "id": "O1",\n'
    b'      "op": "edit_string",\n'
    b'      "path": [\n'
    b'        "note"\n'
    b'      ],\n'
    b'      "anchor": "old",\n'
    b'      "replacement": "new"\n'
    b'    }\n'
    b'  ]\n'
    b'}\n'
)
DRAFT_OTHER = (b'{"changeset_format": "json-ops-v1", "document": "report.json", '
               b'"request_id": "req-2", "operations": []}')
DEEP_SOURCE = b'{\n  "x": {\n    "y": {\n      "z": {\n        "tail": []\n      }\n    }\n  }\n}\n'
RENDERER = 'json-2space-unicode-lf1'
SENTINEL_BATCH = 'SENTINEL-BATCH-77'
SENTINEL_COND = 'SENTINEL-COND'


def _tool():
    import changeset_tool
    return changeset_tool


class PlanApiTests(unittest.TestCase):
    def setUp(self):
        if os.name != 'posix':
            self.skipTest('plan API requires POSIX descriptors')
        base = os.path.realpath(tempfile.gettempdir())
        self.workspace = tempfile.mkdtemp(prefix='csplan-ws-', dir=base)
        self.state = tempfile.mkdtemp(prefix='csplan-state-', dir=base)
        self.inputs = tempfile.mkdtemp(prefix='csplan-in-', dir=base)
        for path in (self.workspace, self.state, self.inputs):
            self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        self.tool = _tool()

    def write(self, path, data, mode=0o644):
        parent = os.path.dirname(path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, mode=0o700)
        with open(path, 'wb') as handle:
            handle.write(data)
        os.chmod(path, mode)

    def read(self, path):
        with open(path, 'rb') as handle:
            return handle.read()

    def prepare(self):
        self.write(os.path.join(self.workspace, 'report.json'), SOURCE, 0o644)
        self.write(os.path.join(self.inputs, 'draft.json'), DRAFT)

    def envelope(self, target='report.json', source=SOURCE, draft_locator='draft.json',
                 draft_bytes=DRAFT, conditions=None, renderer=RENDERER, batch=SENTINEL_BATCH,
                 source_decl=None, draft_decl=None, extra=None):
        if source_decl is None:
            info = os.stat(os.path.join(self.workspace, target))
            source_decl = {'bytes': len(source), 'sha256': sha256(source),
                           'mode': stat.S_IMODE(info.st_mode)}
        if draft_decl is None:
            draft_decl = {'locator': draft_locator, 'bytes': len(draft_bytes),
                          'sha256': sha256(draft_bytes)}
        env = {
            'envelope_version': 1,
            'batch_id': batch,
            'execution': 'sequential-v1',
            'targets': [{'target': target, 'source': source_decl, 'draft': draft_decl,
                         'renderer': renderer}],
            'conditions': list(conditions) if conditions is not None else [],
        }
        if extra:
            env.update(extra)
        return env

    def plan(self, envelope, supplied=None):
        raw = canonical(envelope).encode('utf-8')
        try:
            return self.tool.plan(self.workspace, self.state, self.inputs, raw, supplied)
        except ChangesetError as exc:
            if exc.code in ('unsupported_platform', 'unsupported_capability'):
                self.skipTest('host lacks required primitives: ' + exc.code)
            raise

    def assertPlanRefused(self, code, envelope, supplied=None):
        with self.assertRaises(ChangesetError) as caught:
            self.plan(envelope, supplied)
        self.assertEqual(caught.exception.code, code)

    def load_plan(self, plan_sha):
        plan = strict_loads(self.read(os.path.join(self.state, 'plans', plan_sha + '.json')))
        self.assertEqual(plan['plan_sha256'], plan_sha)
        self.assertEqual(digest_object(plan, 'plan_sha256'), plan_sha)
        return plan

    def template(self, plan):
        return strict_loads(self.read(os.path.join(self.state, 'templates',
                                                   plan['plan_sha256'] + '.json')))

    def test_plan_writes_exact_proposed_source_and_draft_blobs(self):
        self.prepare()
        summary = self.plan(self.envelope())
        self.assertEqual(summary['outcome'], 'planned')
        self.assertEqual(sorted(os.listdir(self.workspace)), ['report.json'])
        plan = self.load_plan(summary['plan_sha256'])
        target = plan['targets'][0]
        self.assertEqual(plan['stores'], {'staged': 'staged', 'drafts': 'drafts',
                                          'sources': 'sources'})
        self.assertEqual(target['proposed']['sha256'], sha256(EXPECTED))
        self.assertEqual(target['proposed']['bytes'], len(EXPECTED))
        self.assertEqual(self.read(os.path.join(self.state, 'staged', sha256(EXPECTED))),
                         EXPECTED)
        self.assertEqual(self.read(os.path.join(self.state, 'drafts', sha256(DRAFT))), DRAFT)
        self.assertEqual(self.read(os.path.join(self.state, 'sources', sha256(SOURCE))), SOURCE)
        self.assertEqual(target['draft']['locator'], 'draft.json')
        self.assertEqual(target['draft']['sha256'], sha256(DRAFT))
        self.assertEqual(target['draft']['blob'], 'drafts/' + sha256(DRAFT))
        self.assertEqual(target['blobs']['staged'], 'staged/' + sha256(EXPECTED))
        self.assertEqual(target['source']['xattr_count'], len(target['source']['xattrs']))
        self.assertEqual(target['source']['xattr_bytes'], target['metadata']['xattr_bytes'])
        for item in target['source']['xattrs']:
            self.assertEqual(set(item), {'name', 'value'})
        self.assertEqual(summary['evidence']['staged_set_sha256'], staged_set_digest(plan))

    def test_summary_exposes_only_digests(self):
        self.prepare()
        summary = self.plan(self.envelope())
        blob = json.dumps(summary)
        self.assertNotIn(SENTINEL_BATCH, blob)
        self.assertNotIn(self.workspace, blob)
        self.assertEqual(summary['batch_id_sha256'], sha256(SENTINEL_BATCH.encode('utf-8')))

    def test_rehashed_malformed_saved_plans_refuse_without_candidate_writes(self):
        # An operator-supplied digest cannot turn malformed execution data into
        # a valid plan. Exercise the public apply boundary, not only the parser.
        self.prepare()
        original = self.load_plan(self.plan(self.envelope())['plan_sha256'])
        cases = [
            ('missing_targets', lambda p: p.pop('targets')),
            ('extra_field', lambda p: p.update(callback='do-something')),
            ('empty_targets', lambda p: p.update(targets=[])),
            ('boolean_version', lambda p: p.update(plan_version=True)),
            ('boolean_root_inode', lambda p: p['workspace'].update(inode=True)),
            ('float_root_inode', lambda p: p['workspace'].update(inode=float(p['workspace']['inode']))),
            ('float_source_device', lambda p: p['targets'][0]['source'].update(device=1.0)),
            ('store_escape', lambda p: p['stores'].update(staged='../elsewhere')),
            ('blob_escape', lambda p: p['targets'][0]['blobs'].update(source='../elsewhere')),
            ('unknown_origin', lambda p: p['targets'][0]['draft'].update(origin='optional')),
            ('boolean_count', lambda p: p['targets'][0]['metadata'].update(xattr_count=False)),
            ('float_count', lambda p: p['targets'][0]['metadata'].update(xattr_count=0.0)),
            ('duplicate_target', lambda p: p['targets'].append(copy.deepcopy(p['targets'][0]))),
            ('duplicate_operation', lambda p: p['targets'][0].update(operation_ids=['O1', 'O1'])),
            ('invented_input', lambda p: p['pinned_inputs'].append({'input_id': 'invented'})),
            ('unbounded_file', lambda p: p['targets'][0]['source'].update(bytes=10 * 1024 * 1024 + 1)),
            ('metadata_disagreement', lambda p: p['targets'][0]['source'].update(mode=0o600)),
            ('root_traversal', lambda p: p['state_root'].update(path=self.state + '/../elsewhere')),
        ]
        before = os.stat(os.path.join(self.workspace, 'report.json'))
        for name, mutate in cases:
            with self.subTest(case=name):
                changed = copy.deepcopy(original)
                mutate(changed)
                # Bypass the production structural serializer to model a
                # malformed record rehashed externally, including floats.
                unsigned = {k: v for k, v in changed.items() if k != 'plan_sha256'}
                encode = lambda value: json.dumps(value, sort_keys=True, separators=(',', ':'),
                                                  ensure_ascii=True, allow_nan=False).encode('utf-8')
                changed['plan_sha256'] = sha256(encode(unsigned))
                self.write(os.path.join(self.state, 'plans', changed['plan_sha256'] + '.json'),
                           encode(changed), 0o600)
                with self.assertRaises(ChangesetError) as caught:
                    self.tool.apply(self.workspace, self.state, self.inputs,
                                    changed['plan_sha256'], receipts=b'{}')
                self.assertEqual(caught.exception.code, 'plan_schema_invalid')
                self.assertEqual(self.read(os.path.join(self.workspace, 'report.json')), SOURCE)
                self.assertEqual(os.stat(os.path.join(self.workspace, 'report.json')).st_ino,
                                 before.st_ino)
                self.assertEqual(os.listdir(self.workspace), ['report.json'])

    def test_historical_fallback_checks_stay_external_and_distinct(self):
        source = b'{\n  "note": "hello old world",\n  "fallbacks": [\n    "known-old-entry"\n  ]\n}\n'
        expected = b'{\n  "note": "hello new world",\n  "fallbacks": [\n    "known-old-entry"\n  ]\n}\n'
        self.write(os.path.join(self.workspace, 'report.json'), source)
        self.write(os.path.join(self.inputs, 'draft.json'), DRAFT)
        # This is the independent project-specific checker, not a callback
        # executed by the generic changeset runtime.
        old_fallbacks = {'known-old-entry'}
        for rule, passing in (('zero-fallback', False), ('no-new-fallback', True)):
            condition = {'id': rule, 'phase': 'pre_apply', 'text': rule,
                         'description': 'Independently inspect the proposed fallback inventory',
                         'inputs': []}
            plan = self.load_plan(self.plan(self.envelope(source=source, batch=rule,
                                                         conditions=[condition]))['plan_sha256'])
            staged = self.read(os.path.join(self.state, 'staged', sha256(expected)))
            self.assertEqual(staged, expected)
            new_fallbacks = set(json.loads(staged)['fallbacks'])
            checked = not new_fallbacks if rule == 'zero-fallback' else new_fallbacks <= old_fallbacks
            self.assertEqual(checked, passing)
            receipt = self.template(plan)
            receipt['conditions'][0].update(result='pass' if checked else 'fail',
                                             evidence='External set comparison in this test')
            if passing:
                result = self.tool.apply(self.workspace, self.state, self.inputs,
                                         plan['plan_sha256'], receipts=canonical(receipt).encode())
                self.assertEqual(result['outcome'], 'applied_unverified')
                self.assertEqual(result['coverage']['acceptance'], 'unknown')
                self.assertEqual(self.read(os.path.join(self.workspace, 'report.json')), expected)
            else:
                with self.assertRaises(ChangesetError) as caught:
                    self.tool.apply(self.workspace, self.state, self.inputs,
                                    plan['plan_sha256'], receipts=canonical(receipt).encode())
                self.assertEqual(caught.exception.code, 'receipt_not_passing')
                self.assertEqual(self.read(os.path.join(self.workspace, 'report.json')), source)

    def test_no_condition_template_is_a_valid_receipt_copy(self):
        self.prepare()
        plan = self.load_plan(self.plan(self.envelope())['plan_sha256'])
        template_bytes = self.read(os.path.join(self.state, 'templates',
                                                plan['plan_sha256'] + '.json'))
        template = strict_loads(template_bytes)
        self.assertEqual(template['receipt_version'], 1)
        self.assertNotIn('receipt_template_version', template)
        self.assertEqual(template['conditions'], [])
        self.assertEqual(template['input_digests'], [])
        self.assertEqual(template['plan_sha256'], plan['plan_sha256'])
        self.assertEqual(template['staged_set_sha256'], staged_set_digest(plan))
        self.assertEqual(validate_receipt(template_bytes, plan),
                         {'sha256': sha256(template_bytes), 'conditions': []})

    def test_supplied_draft_is_durably_bound(self):
        self.write(os.path.join(self.workspace, 'report.json'), SOURCE, 0o644)
        env = self.envelope(draft_locator='supplied/draft.json')
        plan = self.load_plan(self.plan(env, {'supplied/draft.json': DRAFT})['plan_sha256'])
        self.assertEqual(plan['targets'][0]['draft']['sha256'], sha256(DRAFT))
        self.assertEqual(self.read(os.path.join(self.state, 'drafts', sha256(DRAFT))), DRAFT)

    def test_conflicting_locator_file_and_supplied_draft_refuse(self):
        self.prepare()
        self.write(os.path.join(self.inputs, 'draft.json'), DRAFT_OTHER)
        self.assertPlanRefused('draft_locator_conflict', self.envelope(), {'draft.json': DRAFT})

    def test_draft_pin_mismatch_and_missing_draft_refuse(self):
        self.write(os.path.join(self.workspace, 'report.json'), SOURCE, 0o644)
        self.write(os.path.join(self.inputs, 'draft.json'), DRAFT_OTHER)
        self.assertPlanRefused('draft_pin_mismatch', self.envelope())
        os.remove(os.path.join(self.inputs, 'draft.json'))
        self.assertPlanRefused('draft_missing', self.envelope())

    def test_pinned_input_is_bound_and_streamed(self):
        self.prepare()
        self.write(os.path.join(self.inputs, 'pins', 'a.txt'), b'abc')
        condition = {'id': SENTINEL_COND, 'phase': 'pre_apply', 'text': 'check a',
                     'description': 'synthetic pin',
                     'inputs': [{'id': 'in-1', 'root': 'input', 'path': 'pins/a.txt',
                                 'bytes': 3, 'sha256': sha256(b'abc')}]}
        env = self.envelope(draft_locator='supplied/draft.json', conditions=[condition])
        import changeset_plan
        with mock.patch.object(changeset_plan, 'read_file_at',
                               side_effect=AssertionError('pinned inputs must stream')):
            summary = self.plan(env, {'supplied/draft.json': DRAFT})
        plan = self.load_plan(summary['plan_sha256'])
        self.assertEqual(plan['pinned_inputs'],
                         [{'condition_id': SENTINEL_COND, 'input_id': 'in-1', 'root': 'input',
                           'path': 'pins/a.txt', 'bytes': 3, 'sha256': sha256(b'abc')}])
        template = self.template(plan)
        self.assertEqual(template['input_digests'],
                         [{'condition_id': SENTINEL_COND, 'input_id': 'in-1',
                           'sha256': sha256(b'abc')}])
        with self.assertRaises(ChangesetError) as caught:
            validate_receipt(canonical(template).encode('utf-8'), plan)
        self.assertEqual(caught.exception.code, 'receipt_not_passing')
        filled = dict(template)
        filled['conditions'] = [dict(template['conditions'][0], result='pass')]
        result = validate_receipt(canonical(filled).encode('utf-8'), plan)
        self.assertEqual(result['conditions'], [sha256(SENTINEL_COND.encode('utf-8'))])
        self.assertNotIn(SENTINEL_COND, json.dumps(result))

    def test_input_pin_mismatch_and_alias_refuse(self):
        self.prepare()
        self.write(os.path.join(self.inputs, 'pins', 'a.txt'), b'abc')
        bad = {'id': 'in-1', 'root': 'input', 'path': 'pins/a.txt', 'bytes': 3,
               'sha256': 'd' * 64}
        self.assertPlanRefused('input_pin_mismatch', self.envelope(conditions=[{
            'id': SENTINEL_COND, 'phase': 'pre_apply', 'text': 'check', 'description': 'pin',
            'inputs': [bad]}]))
        alias = {'id': 'in-2', 'root': 'workspace', 'path': 'report.json',
                 'bytes': len(SOURCE), 'sha256': sha256(SOURCE)}
        self.assertPlanRefused('input_aliases_target', self.envelope(conditions=[{
            'id': SENTINEL_COND, 'phase': 'pre_apply', 'text': 'check',
            'description': 'alias', 'inputs': [alias]}]))

    def test_duplicate_input_ids_refuse(self):
        self.prepare()
        self.write(os.path.join(self.inputs, 'pins', 'a.txt'), b'abc')
        self.write(os.path.join(self.inputs, 'pins', 'b.txt'), b'abc')
        inputs = [
            {'id': 'dup', 'root': 'input', 'path': 'pins/a.txt', 'bytes': 3,
             'sha256': sha256(b'abc')},
            {'id': 'dup', 'root': 'input', 'path': 'pins/b.txt', 'bytes': 3,
             'sha256': sha256(b'abc')},
        ]
        self.assertPlanRefused('duplicate_input_id', self.envelope(conditions=[{
            'id': SENTINEL_COND, 'phase': 'pre_apply', 'text': 'check',
            'description': 'dup ids', 'inputs': inputs}]))

    def test_pinned_input_count_and_size_bounds(self):
        self.prepare()
        for index in range(65):
            self.write(os.path.join(self.inputs, 'pins', 'i%d.txt' % index), b'x')

        def condition(count):
            inputs = [{'id': 'in-%d' % index, 'root': 'input',
                       'path': 'pins/i%d.txt' % index, 'bytes': 1,
                       'sha256': sha256(b'x')} for index in range(count)]
            return {'id': SENTINEL_COND, 'phase': 'pre_apply', 'text': 'check',
                    'description': 'bulk pins', 'inputs': inputs}

        plan = self.load_plan(self.plan(self.envelope(conditions=[condition(64)]))['plan_sha256'])
        self.assertEqual(len(plan['pinned_inputs']), 64)
        self.assertPlanRefused('too_many_pinned_inputs', self.envelope(conditions=[condition(65)]))
        oversized = {'id': 'in-big', 'root': 'input', 'path': 'pins/i0.txt',
                     'bytes': 10 * 1024 * 1024 + 1, 'sha256': sha256(b'x')}
        self.assertPlanRefused('file_too_large', self.envelope(conditions=[{
            'id': SENTINEL_COND, 'phase': 'pre_apply', 'text': 'check',
            'description': 'oversized pin', 'inputs': [oversized]}]))

    def test_actual_pinned_input_sizes_refuse_without_plan_or_candidate_write(self):
        self.prepare()
        import changeset_plan
        pin_path = os.path.join(self.inputs, 'pin.bin')
        before = os.stat(os.path.join(self.workspace, 'report.json'))
        for actual_size, declared_size, code in (
                (4, 3, 'input_pin_mismatch'),
                (10 * 1024 * 1024 + 1, 10 * 1024 * 1024, 'file_too_large')):
            with self.subTest(actual_size=actual_size):
                with open(pin_path, 'wb') as handle:
                    handle.truncate(actual_size)
                condition = {'id': 'size-check', 'phase': 'pre_apply', 'text': 'check',
                             'description': 'actual-size bound', 'inputs': [
                                 {'id': 'pin', 'root': 'input', 'path': 'pin.bin',
                                  'bytes': declared_size, 'sha256': '0' * 64}]}
                with mock.patch.object(changeset_plan, '_write_plan_files',
                                       side_effect=AssertionError('No plan may commit before pin validation')):
                    self.assertPlanRefused(code, self.envelope(conditions=[condition]))
                self.assertEqual(self.read(os.path.join(self.workspace, 'report.json')), SOURCE)
                self.assertEqual(os.stat(os.path.join(self.workspace, 'report.json')).st_ino,
                                 before.st_ino)
                self.assertEqual(os.listdir(self.workspace), ['report.json'])
                # Refused planning may preserve private content-addressed
                # target blobs. It never persists a plan/template or copies
                # the extra check input, and never writes the candidate.
                for store in ('plans', 'templates'):
                    path = os.path.join(self.state, store)
                    self.assertEqual(os.listdir(path) if os.path.isdir(path) else [], [])
                retained = sum(os.stat(os.path.join(self.state, store, name)).st_size
                               for store in ('sources', 'drafts', 'staged')
                               for name in os.listdir(os.path.join(self.state, store)))
                self.assertEqual(retained, len(SOURCE) + len(DRAFT) + len(EXPECTED))

    def test_condition_schema_is_enforced(self):
        self.prepare()
        base = {'id': SENTINEL_COND, 'phase': 'pre_apply', 'text': 'check',
                'description': 'desc', 'inputs': []}
        post = {'id': 'post-1', 'phase': 'post_apply', 'text': 'check',
                'description': 'desc', 'inputs': []}
        cases = (
            ('missing_post_apply_explanation', post),
            ('invalid_pre_apply_condition', dict(base, failure_handling='x')),
            ('invalid_condition', dict(base, extra='x')),
        )
        for code, condition in cases:
            self.assertPlanRefused(code, self.envelope(conditions=[condition]))
        self.assertPlanRefused('duplicate_condition_id',
                               self.envelope(conditions=[base, dict(base)]))
        many = [dict(base, id='c%d' % index) for index in range(257)]
        self.assertPlanRefused('invalid_conditions', self.envelope(conditions=many))

    def test_envelope_and_source_bounds(self):
        self.prepare()
        self.assertPlanRefused('invalid_envelope', self.envelope(extra={'execute': True}))
        self.assertPlanRefused('invalid_envelope_version',
                               self.envelope(extra={'envelope_version': 2}))
        self.assertPlanRefused('invalid_execution',
                               self.envelope(extra={'execution': 'parallel-v1'}))
        dummy = {'target': 'report.json',
                 'source': {'bytes': 1, 'sha256': 'a' * 64, 'mode': 0o644},
                 'draft': {'locator': 'draft.json', 'bytes': 1, 'sha256': 'a' * 64},
                 'renderer': RENDERER}
        env = self.envelope()
        env['targets'] = [dict(dummy) for _ in range(33)]
        self.assertPlanRefused('invalid_targets', env)
        env = self.envelope()
        env['targets'] = [env['targets'][0], dict(env['targets'][0])]
        self.assertPlanRefused('duplicate_target', env)
        self.assertPlanRefused('invalid_relative_path', self.envelope(
            target='a/../report.json',
            source_decl={'bytes': 1, 'sha256': 'a' * 64, 'mode': 0o644}))
        self.assertPlanRefused('source_mode_mismatch', self.envelope(
            source_decl={'bytes': len(SOURCE), 'sha256': sha256(SOURCE), 'mode': 0o600}))
        self.assertPlanRefused('source_pin_mismatch', self.envelope(
            source_decl={'bytes': len(SOURCE), 'sha256': 'a' * 64, 'mode': 0o644}))
        self.assertPlanRefused('file_missing', self.envelope(
            target='absent.json',
            source_decl={'bytes': 1, 'sha256': 'a' * 64, 'mode': 0o644}))

    def test_operation_schema_is_enforced_through_plan(self):
        self.prepare()
        unknown = {'changeset_format': 'json-ops-v1', 'document': 'report.json',
                   'request_id': 'q',
                   'operations': [{'id': 'O1', 'op': 'edit_string', 'path': ['note'],
                                   'anchor': 'old', 'replacement': 'new', 'shell': 'no'}]}
        raw = json.dumps(unknown).encode('utf-8')
        self.write(os.path.join(self.inputs, 'draft.json'), raw)
        self.assertPlanRefused('unknown_operation_field', self.envelope(draft_bytes=raw))
        missing = {'changeset_format': 'json-ops-v1', 'document': 'report.json',
                   'request_id': 'q',
                   'operations': [{'id': 'O1', 'op': 'edit_string', 'path': ['note']}]}
        raw = json.dumps(missing).encode('utf-8')
        self.write(os.path.join(self.inputs, 'draft.json'), raw)
        self.assertPlanRefused('missing_operation_field', self.envelope(draft_bytes=raw))
        annotations = {'changeset_format': 'json-ops-v1', 'document': 'report.json',
                       'request_id': 'q',
                       'operations': [{'id': 'O1', 'op': 'edit_string', 'path': ['note'],
                                       'anchor': 'old', 'replacement': 'new',
                                       'location': 'doc.md', 'rationale': 'inert'}]}
        raw = json.dumps(annotations).encode('utf-8')
        self.write(os.path.join(self.inputs, 'draft.json'), raw)
        plan = self.load_plan(self.plan(self.envelope(draft_bytes=raw))['plan_sha256'])
        staged = self.read(os.path.join(self.state, 'staged',
                                        plan['targets'][0]['proposed']['sha256']))
        self.assertEqual(staged, EXPECTED)

    def test_aggregate_bound_is_enforced(self):
        self.prepare()
        plan = self.load_plan(self.plan(self.envelope())['plan_sha256'])
        total = len(SOURCE) + len(DRAFT) + len(EXPECTED) \
            + plan['targets'][0]['source']['xattr_bytes']
        import changeset_plan
        with mock.patch.object(changeset_plan, 'MAX_AGGREGATE_BYTES', total):
            self.assertEqual(self.plan(self.envelope())['outcome'], 'planned')
        with mock.patch.object(changeset_plan, 'MAX_AGGREGATE_BYTES', total - 1):
            self.assertPlanRefused('aggregate_bytes_exceeded', self.envelope())

    def test_proposed_depth_cap_via_plan(self):
        self.write(os.path.join(self.workspace, 'report.json'), DEEP_SOURCE, 0o644)

        def draft_bytes(levels):
            nested = 'leaf'
            for _ in range(levels):
                nested = [nested]
            draft = {'changeset_format': 'json-ops-v1', 'document': 'report.json',
                     'request_id': 'req-deep',
                     'operations': [{'id': 'D1', 'op': 'append_items',
                                     'path': ['x', 'y', 'z', 'tail'], 'old_length': 0,
                                     'items': [nested]}]}
            return json.dumps(draft).encode('utf-8')

        allowed = draft_bytes(59)
        self.write(os.path.join(self.inputs, 'draft.json'), allowed)
        plan = self.load_plan(self.plan(self.envelope(source=DEEP_SOURCE,
                                                     draft_bytes=allowed))['plan_sha256'])
        blob = self.read(os.path.join(self.state, 'staged',
                                      plan['targets'][0]['proposed']['sha256']))
        self.assertEqual(sha256(blob), plan['targets'][0]['proposed']['sha256'])
        refused = draft_bytes(60)
        self.write(os.path.join(self.inputs, 'draft.json'), refused)
        self.assertPlanRefused('proposed_too_deep',
                               self.envelope(source=DEEP_SOURCE, draft_bytes=refused))


if __name__ == '__main__':
    unittest.main()
