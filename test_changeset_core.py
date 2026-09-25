"""Literal-oracle tests for changeset_core / changeset_ops.

Expected bytes are transcribed from the independent fixture manifest
(changeset-applier-100/fixtures/MANIFEST.json) exactly, including trailing
newlines and the absent final newline of the CRLF fixture.  No expected value
in this file is produced by the production renderer.  Pure on any platform.
"""

import unittest

from changeset_core import (
    MAX_FILE_BYTES, MAX_JSON_DEPTH, ChangesetError, check_id, render_json,
    strict_loads, typed_equal, value_depth_ok,
)
from changeset_ops import simulate, validate_draft

ALL_JSON_OPS_BEFORE = (
    b'{\n'
    b'  "title": "prefix old suffix",\n'
    b'  "whole": "start untouched interior end",\n'
    b'  "flag": true,\n'
    b'  "rows": [\n'
    b'    {\n'
    b'      "id": "a",\n'
    b'      "n": 1\n'
    b'    },\n'
    b'    {\n'
    b'      "id": "z",\n'
    b'      "n": 2\n'
    b'    }\n'
    b'  ],\n'
    b'  "tail": []\n'
    b'}\n'
)
ALL_JSON_OPS_EXPECTED = (
    b'{\n'
    b'  "title": "prefix revised suffix",\n'
    b'  "whole": "replacement",\n'
    b'  "flag": true,\n'
    b'  "added": "caf\xc3\xa9",\n'
    b'  "rows": [\n'
    b'    {\n'
    b'      "id": "a",\n'
    b'      "n": 1\n'
    b'    },\n'
    b'    {\n'
    b'      "id": "b",\n'
    b'      "n": 4\n'
    b'    },\n'
    b'    {\n'
    b'      "id": "z",\n'
    b'      "n": 3\n'
    b'    }\n'
    b'  ],\n'
    b'  "tail": [\n'
    b'    "first",\n'
    b'    "second"\n'
    b'  ]\n'
    b'}\n'
)
ALL_JSON_OPS = [
    {'id': 'J1', 'op': 'edit_string', 'path': ['title'], 'anchor': 'old', 'replacement': 'revised'},
    {'id': 'J2', 'op': 'replace_string', 'path': ['whole'], 'old_starts_with': 'start',
     'old_ends_with': 'end', 'new': 'replacement'},
    {'id': 'J3', 'op': 'insert_key_after', 'path': [], 'after_key': 'flag', 'key': 'added',
     'value': 'caf\u00e9'},
    {'id': 'J4', 'op': 'insert_items_after', 'path': ['rows'], 'after': {'id': 'a'},
     'items': [{'id': 'b', 'n': 4}]},
    {'id': 'J5', 'op': 'set_value', 'path': ['rows', {'id': 'z'}, 'n'], 'old': 2, 'new': 3},
    {'id': 'J6', 'op': 'append_items', 'path': ['tail'], 'old_length': 0,
     'items': ['first', 'second']},
]
UNICODE_CRLF_BEFORE = b'caf\xc3\xa9\r\nstatus: old\r\nlast line'
UNICODE_CRLF_EXPECTED = b'caf\xc3\xa9\r\nstatus: revised\r\nlast line'
UNICODE_CRLF_OPERATIONS = [{'id': 'T1', 'anchor': 'status: old', 'replacement': 'status: revised'}]
LF0 = 'json-2space-unicode-lf0'
LF1 = 'json-2space-unicode-lf1'
JSON_SIMPLE = b'{\n  "v": 1\n}'
JSON_TYPED = b'{\n  "v": {\n    "ok": true\n  }\n}'
JSON_IDS = b'{\n  "v": [\n    {\n      "id": "a"\n    }\n  ]\n}'
JSON_DUP_SELECTOR = b'{\n  "v": [\n    {\n      "id": "a"\n    },\n    {\n      "id": "a"\n    }\n  ]\n}'
JSON_BOOL_INDEX = b'{\n  "v": [\n    1,\n    2\n  ]\n}'
JSON_TAIL = b'{\n  "tail": []\n}'
DUP_ARRAYS = (
    b'{\n'
    b'  "v": [\n'
    b'    {\n'
    b'      "id": "a"\n'
    b'    },\n'
    b'    {\n'
    b'      "id": "b"\n'
    b'    }\n'
    b'  ],\n'
    b'  "other": [\n'
    b'    {\n'
    b'      "id": "x"\n'
    b'    },\n'
    b'    {\n'
    b'      "id": "x"\n'
    b'    }\n'
    b'  ],\n'
    b'  "n": 1\n'
    b'}'
)
NESTED_KIDS = (
    b'{\n'
    b'  "v": [\n'
    b'    {\n'
    b'      "id": "a",\n'
    b'      "kids": [\n'
    b'        {\n'
    b'          "id": "k1"\n'
    b'        },\n'
    b'        {\n'
    b'          "id": "k2"\n'
    b'        }\n'
    b'      ]\n'
    b'    }\n'
    b'  ]\n'
    b'}'
)


class BoundedCase(unittest.TestCase):
    def assertRefused(self, code, function, *args, **kwargs):
        with self.assertRaises(ChangesetError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)


class LiteralFixtureTests(BoundedCase):
    def test_all_json_ops_fixture_exact_bytes(self):
        result = simulate('json-ops-v1', ALL_JSON_OPS_BEFORE, ALL_JSON_OPS, LF1)
        self.assertEqual(result, ALL_JSON_OPS_EXPECTED)
        self.assertEqual(len(ALL_JSON_OPS_BEFORE), 210)
        self.assertEqual(len(ALL_JSON_OPS_EXPECTED), 289)

    def test_unicode_crlf_fixture_exact_bytes(self):
        result = simulate('anchored-text-v1', UNICODE_CRLF_BEFORE, UNICODE_CRLF_OPERATIONS, None)
        self.assertEqual(result, UNICODE_CRLF_EXPECTED)
        self.assertEqual(len(UNICODE_CRLF_BEFORE), 29)
        self.assertEqual(len(UNICODE_CRLF_EXPECTED), 33)
        self.assertFalse(result.endswith(b'\n'))
        self.assertIn(b'\r\n', result)

    def test_noop_and_empty_operations_keep_exact_bytes(self):
        noops = [
            {'id': 'N1', 'op': 'set_value', 'path': ['flag'], 'old': True, 'new': True},
            {'id': 'N2', 'op': 'append_items', 'path': ['tail'], 'old_length': 0, 'items': []},
            {'id': 'N3', 'op': 'edit_string', 'path': ['title'], 'anchor': 'old', 'replacement': 'old'},
        ]
        self.assertEqual(simulate('json-ops-v1', ALL_JSON_OPS_BEFORE, noops, LF1),
                         ALL_JSON_OPS_BEFORE)
        self.assertEqual(simulate('json-ops-v1', ALL_JSON_OPS_BEFORE, [], LF1),
                         ALL_JSON_OPS_BEFORE)
        self.assertEqual(simulate('anchored-text-v1', UNICODE_CRLF_BEFORE, [], None),
                         UNICODE_CRLF_BEFORE)

    def test_render_profiles_are_exact_literals(self):
        self.assertEqual(render_json({'a': 1}, 'json-2space-unicode-lf1'), b'{\n  "a": 1\n}\n')
        self.assertEqual(render_json({'a': 1}, 'json-2space-unicode-lf0'), b'{\n  "a": 1\n}')
        self.assertEqual(render_json({'a': '\u00e9'}, 'json-2space-unicode-lf0'),
                         b'{\n  "a": "\xc3\xa9"\n}')
        self.assertEqual(render_json({'a': '\u00e9'}, 'json-2space-ascii-lf0'),
                         b'{\n  "a": "\\u00e9"\n}')
        self.assertEqual(render_json({'a': '\u00e9'}, 'json-2space-ascii-lf1'),
                         b'{\n  "a": "\\u00e9"\n}\n')
        self.assertRefused('invalid_renderer', render_json, {'a': 1}, 'json-4space')
        self.assertRefused('invalid_unicode_surrogate', render_json, {'a': '\ud800'}, LF0)

    def test_unrelated_formatting_refuses_before_staging(self):
        four_space = b'{\n    "note": "x"\n}'
        self.assertRefused('formatting_mismatch', simulate, 'json-ops-v1', four_space, [], LF0)
        self.assertRefused('renderer_not_applicable', simulate, 'anchored-text-v1', b'x', [], LF0)


class StrictJsonTests(BoundedCase):
    def test_finite_float_literal_uses_file_bound_not_integer_digit_bound(self):
        # The integer-only 4300-digit limit must not reject valid finite
        # decimal mantissas that remain within the overall JSON byte bound.
        self.assertEqual(strict_loads(b'0.1' + b'0' * 5000), 0.1)

    def test_rejects_duplicates_bom_utf8_surrogates(self):
        self.assertRefused('duplicate_json_key', strict_loads, b'{"v":1,"v":2}')
        self.assertRefused('duplicate_json_key', strict_loads, b'{"a":{"v":1,"v":2}}')
        self.assertRefused('utf8_bom', strict_loads, b'\xef\xbb\xbf{}')
        self.assertRefused('invalid_utf8', strict_loads, b'{"v":"\xff"}')
        self.assertRefused('invalid_unicode_surrogate', strict_loads, b'{"v":"\\ud800"}')
        self.assertRefused('invalid_unicode_surrogate', strict_loads, '{"v":"\ud800"}')

    def test_rejects_nonfinite_and_underflow(self):
        self.assertRefused('nonfinite_number', strict_loads, b'{"v":NaN}')
        self.assertRefused('nonfinite_number', strict_loads, b'{"v":Infinity}')
        self.assertRefused('nonfinite_number', strict_loads, b'{"v":1e400}')
        self.assertRefused('float_underflow', strict_loads, b'{"v":1e-4000}')
        self.assertEqual(strict_loads(b'{"v":0.0}')['v'], 0.0)
        self.assertEqual(strict_loads(b'{"v":-0.0}')['v'], -0.0)

    def test_integer_digit_bound(self):
        allowed = strict_loads(b'{"v":' + b'1' * 4300 + b'}')
        self.assertIsInstance(allowed['v'], int)
        self.assertRefused('integer_too_long', strict_loads, b'{"v":' + b'1' * 4301 + b'}')

    def test_depth_bound_is_iterative(self):
        strict_loads(b'[' * 64 + b'0' + b']' * 64)
        self.assertRefused('json_too_deep', strict_loads, b'[' * 65 + b'0' + b']' * 65)
        self.assertFalse(value_depth_ok([[[[0]]]], 3))
        self.assertTrue(value_depth_ok([[[[0]]]], 4))
        value = 0
        for _ in range(MAX_JSON_DEPTH):
            value = [value]
        self.assertTrue(value_depth_ok(value, MAX_JSON_DEPTH))
        self.assertFalse(value_depth_ok([value], MAX_JSON_DEPTH))

    def test_limit_bound(self):
        self.assertRefused('input_too_large', strict_loads, b'{"v":1}', limit=4)

    def test_typed_equality(self):
        self.assertFalse(typed_equal(True, 1))
        self.assertFalse(typed_equal(1, 1.0))
        self.assertFalse(typed_equal(0.0, -0.0))
        self.assertFalse(typed_equal([True], [1]))
        self.assertFalse(typed_equal({'ok': True}, {'ok': 1}))
        self.assertTrue(typed_equal(1.0, 1.0))
        self.assertTrue(typed_equal({'a': 1, 'b': [2, {'c': 3}]},
                                    {'b': [2, {'c': 3}], 'a': 1}))

    def test_check_id_bounds(self):
        check_id('a' * 128, 'operation_id')
        self.assertRefused('operation_id_too_long', check_id, 'a' * 129, 'operation_id')
        self.assertRefused('invalid_operation_id', check_id, '\ud800', 'operation_id')
        self.assertRefused('invalid_operation_id', check_id, '', 'operation_id')


class NegativeFixtureTests(BoundedCase):
    def test_overlapping_anchor_refuses(self):
        self.assertRefused('anchor_not_unique', simulate, 'anchored-text-v1', b'aaa',
                           [{'id': 'a', 'anchor': 'aa', 'replacement': 'b'}], None)

    def test_duplicate_anchor_occurrence_refuses(self):
        self.assertRefused('anchor_not_unique', simulate, 'anchored-text-v1', b'x x',
                           [{'id': 'a', 'anchor': 'x', 'replacement': 'y'}], None)

    def test_missing_anchor_refuses(self):
        self.assertRefused('anchor_not_unique', simulate, 'anchored-text-v1', b'zzz',
                           [{'id': 'a', 'anchor': 'q', 'replacement': 'y'}], None)

    def test_nested_typed_mismatch_refuses(self):
        op = {'id': 'a', 'op': 'set_value', 'path': ['v'], 'old': {'ok': 1}, 'new': None}
        self.assertRefused('old_value_mismatch', simulate, 'json-ops-v1', JSON_TYPED, [op], LF0)

    def test_overflow_and_duplicate_json(self):
        self.assertRefused('nonfinite_number', strict_loads, b'{"v":1e400}')
        self.assertRefused('duplicate_json_key', strict_loads, b'{"v":1,"v":2}')

    def test_duplicate_selector_refuses(self):
        op = {'id': 'a', 'op': 'set_value', 'path': ['v', {'id': 'a'}, 'id'], 'old': 'a', 'new': 'b'}
        self.assertRefused('ambiguous_selector', simulate, 'json-ops-v1', JSON_DUP_SELECTOR,
                           [op], LF0)

    def test_unknown_execution_field_refuses(self):
        op = {'id': 'a', 'op': 'set_value', 'path': ['v'], 'old': 1, 'new': 2,
              'shell': 'DO NOT EXECUTE'}
        self.assertRefused('unknown_operation_field', simulate, 'json-ops-v1', JSON_SIMPLE,
                           [op], LF0)

    def test_inserted_duplicate_id_refuses(self):
        op = {'id': 'a', 'op': 'insert_items_after', 'path': ['v'], 'after': {'id': 'a'},
              'items': [{'id': 'a'}]}
        self.assertRefused('duplicate_id_introduced', simulate, 'json-ops-v1', JSON_IDS, [op], LF0)

    def test_boolean_index_refuses(self):
        op = {'id': 'a', 'op': 'set_value', 'path': ['v', True], 'old': 2, 'new': 3}
        self.assertRefused('invalid_path_element', simulate, 'json-ops-v1', JSON_BOOL_INDEX,
                           [op], LF0)

    def test_negative_index_and_implicit_path_refuse(self):
        op = {'id': 'a', 'op': 'set_value', 'path': ['v', -1], 'old': 2, 'new': 3}
        self.assertRefused('invalid_path_index', simulate, 'json-ops-v1', JSON_BOOL_INDEX,
                           [op], LF0)
        op = {'id': 'a', 'op': 'set_value', 'path': ['missing'], 'old': 1, 'new': 2}
        self.assertRefused('path_missing', simulate, 'json-ops-v1', JSON_SIMPLE, [op], LF0)
        op = {'id': 'a', 'op': 'set_value', 'path': [], 'old': 1, 'new': 2}
        self.assertRefused('empty_path', simulate, 'json-ops-v1', JSON_SIMPLE, [op], LF0)


class OperationSchemaTests(BoundedCase):
    def test_missing_required_fields_are_bounded(self):
        cases = (
            {'id': 'm1', 'op': 'set_value', 'path': ['v']},
            {'id': 'm2', 'op': 'edit_string', 'path': ['v']},
            {'id': 'm3', 'op': 'replace_string', 'path': ['v'], 'old_starts_with': 'a'},
            {'id': 'm4', 'op': 'append_items', 'path': ['v'], 'old_length': 0},
            {'id': 'm5', 'op': 'insert_key_after', 'path': [], 'after_key': 'a'},
            {'id': 'm6', 'op': 'insert_items_after', 'path': ['v'], 'after': {'id': 'a'}},
        )
        for op in cases:
            self.assertRefused('missing_operation_field', simulate, 'json-ops-v1', JSON_SIMPLE,
                               [op], LF0)
        self.assertRefused('missing_operation_field', simulate, 'anchored-text-v1', b'x',
                           [{'id': 'a'}], None)
        self.assertRefused('missing_operation_id', simulate, 'json-ops-v1', JSON_SIMPLE,
                           [{'op': 'set_value', 'path': ['v'], 'old': 1, 'new': 2}], LF0)
        self.assertRefused('invalid_operation', simulate, 'json-ops-v1', JSON_SIMPLE,
                           ['not-an-operation'], LF0)

    def test_unknown_field_and_annotation_types(self):
        op = {'id': 'a', 'op': 'set_value', 'path': ['v'], 'old': 1, 'new': 2, 'location': 5}
        self.assertRefused('invalid_annotation', simulate, 'json-ops-v1', JSON_SIMPLE, [op], LF0)
        op = {'id': 'a', 'op': 'set_value', 'path': ['v'], 'old': 1, 'new': 2, 'extra': 1}
        self.assertRefused('unknown_operation_field', simulate, 'json-ops-v1', JSON_SIMPLE,
                           [op], LF0)
        op = {'id': 'a', 'anchor': 'x', 'replacement': 'y', 'path': ['v']}
        self.assertRefused('unknown_operation_field', simulate, 'anchored-text-v1', b'x',
                           [op], None)

    def test_annotations_are_inert(self):
        annotated = [dict(op, location='doc.md', rationale='because', requirement='req-1')
                     for op in ALL_JSON_OPS]
        self.assertEqual(simulate('json-ops-v1', ALL_JSON_OPS_BEFORE, annotated, LF1),
                         ALL_JSON_OPS_EXPECTED)
        text_annotated = [dict(UNICODE_CRLF_OPERATIONS[0], rationale='text note')]
        self.assertEqual(simulate('anchored-text-v1', UNICODE_CRLF_BEFORE, text_annotated, None),
                         UNICODE_CRLF_EXPECTED)

    def test_insert_key_preserves_order_and_duplicate_key_refuses(self):
        source = b'{\n  "a": 1,\n  "b": 2\n}'
        op = {'id': 'k1', 'op': 'insert_key_after', 'path': [], 'after_key': 'a',
              'key': 'c', 'value': 3}
        self.assertEqual(simulate('json-ops-v1', source, [op], LF0),
                         b'{\n  "a": 1,\n  "c": 3,\n  "b": 2\n}')
        op = {'id': 'k2', 'op': 'insert_key_after', 'path': [], 'after_key': 'a',
              'key': 'b', 'value': 3}
        self.assertRefused('insert_key_mismatch', simulate, 'json-ops-v1', source, [op], LF0)

    def test_replace_string_replaces_whole_value(self):
        source = b'{\n  "whole": "start interior end"\n}'
        op = {'id': 'r1', 'op': 'replace_string', 'path': ['whole'],
              'old_starts_with': 'start', 'old_ends_with': 'end', 'new': 'whole-new'}
        self.assertEqual(simulate('json-ops-v1', source, [op], LF0),
                         b'{\n  "whole": "whole-new"\n}')
        bad = {'id': 'r2', 'op': 'replace_string', 'path': ['whole'],
               'old_starts_with': 'nope', 'old_ends_with': 'end', 'new': 'x'}
        self.assertRefused('boundary_mismatch', simulate, 'json-ops-v1', source, [bad], LF0)

    def test_root_collection_paths(self):
        source = b'[\n  1\n]'
        op = {'id': 'r', 'op': 'append_items', 'path': [], 'old_length': 1, 'items': [2]}
        self.assertEqual(simulate('json-ops-v1', source, [op], LF0), b'[\n  1,\n  2\n]')
        op = {'id': 'r2', 'op': 'append_items', 'path': [], 'old_length': 0, 'items': [2]}
        self.assertRefused('length_mismatch', simulate, 'json-ops-v1', source, [op], LF0)


class DuplicateIdTests(BoundedCase):
    def test_id_change_to_existing_sibling_refuses(self):
        op = {'id': 'o', 'op': 'set_value', 'path': ['v', 1, 'id'], 'old': 'b', 'new': 'a'}
        self.assertRefused('duplicate_id_introduced', simulate, 'json-ops-v1', DUP_ARRAYS,
                           [op], LF0)

    def test_element_replacement_with_duplicate_refuses(self):
        op = {'id': 'o', 'op': 'set_value', 'path': ['v', 1], 'old': {'id': 'b'},
              'new': {'id': 'a'}}
        self.assertRefused('duplicate_id_introduced', simulate, 'json-ops-v1', DUP_ARRAYS,
                           [op], LF0)

    def test_unchanged_preexisting_duplicate_elsewhere_is_allowed(self):
        op = {'id': 'o', 'op': 'set_value', 'path': ['n'], 'old': 1, 'new': 2}
        result = simulate('json-ops-v1', DUP_ARRAYS, [op], LF0)
        self.assertEqual(result, DUP_ARRAYS.replace(b'"n": 1', b'"n": 2'))

    def test_preexisting_duplicate_can_decrease(self):
        op = {'id': 'o', 'op': 'set_value', 'path': ['other', 1, 'id'], 'old': 'x', 'new': 'y'}
        self.assertIn(b'"y"', simulate('json-ops-v1', DUP_ARRAYS, [op], LF0))

    def test_nested_array_duplicate_refuses_only_there(self):
        op = {'id': 'o', 'op': 'set_value', 'path': ['v', 0, 'kids', 1, 'id'],
              'old': 'k2', 'new': 'k1'}
        self.assertRefused('duplicate_id_introduced', simulate, 'json-ops-v1', NESTED_KIDS,
                           [op], LF0)
        ok = {'id': 'o2', 'op': 'set_value', 'path': ['v', 0, 'kids', 1, 'id'],
              'old': 'k2', 'new': 'k3'}
        self.assertIn(b'"k3"', simulate('json-ops-v1', NESTED_KIDS, [ok], LF0))

    def test_insert_key_after_added_id_refuses(self):
        source = b'{\n  "v": [\n    {\n      "id": "a"\n    },\n    {\n      "x": 1\n    }\n  ]\n}'
        op = {'id': 'o', 'op': 'insert_key_after', 'path': ['v', 1], 'after_key': 'x',
              'key': 'id', 'value': 'a'}
        self.assertRefused('duplicate_id_introduced', simulate, 'json-ops-v1', source, [op], LF0)

    def test_whole_array_replacement_with_introduced_duplicate_refuses(self):
        source = b'{\n  "v": [\n    {\n      "id": "a"\n    }\n  ]\n}'
        op = {'id': 'o', 'op': 'set_value', 'path': ['v'], 'old': [{'id': 'a'}],
              'new': [{'id': 'a'}, {'id': 'a'}]}
        self.assertRefused('duplicate_id_introduced', simulate, 'json-ops-v1', source, [op], LF0)

    def test_whole_array_replacement_keeping_duplicate_is_allowed(self):
        source = b'{\n  "v": [\n    {\n      "id": "a"\n    },\n    {\n      "id": "a"\n    }\n  ]\n}'
        value = [{'id': 'a'}, {'id': 'a'}]
        op = {'id': 'o', 'op': 'set_value', 'path': ['v'], 'old': value, 'new': value}
        self.assertEqual(simulate('json-ops-v1', source, [op], LF0), source)

    def test_nested_arrays_of_inserted_items_are_not_scanned(self):
        op = {'id': 'o', 'op': 'append_items', 'path': ['v'], 'old_length': 1,
              'items': [{'id': 'b', 'kids': [{'id': 'k'}, {'id': 'k'}]}]}
        result = simulate('json-ops-v1', JSON_IDS, [op], LF0)
        self.assertIn(b'"b"', result)
        self.assertEqual(result.count(b'"k"'), 2)

    def test_draft_duplicate_operation_ids_refuse(self):
        draft = {'changeset_format': 'anchored-text-v1', 'document': 'notes.md', 'request_id': 'q',
                 'operations': [{'id': 'a', 'anchor': 'x', 'replacement': 'y'},
                                {'id': 'a', 'anchor': 'y', 'replacement': 'z'}]}
        self.assertRefused('duplicate_operation_id', validate_draft, draft, 'notes.md')

    def test_draft_schema_and_advisory_rules(self):
        draft = {'changeset_format': 'anchored-text-v1', 'document': 'notes.md', 'request_id': 'q',
                 'operations': [{'id': 'a', 'anchor': 'x', 'replacement': 'y'}],
                 'self_check': {'claim': 'inert'}, 'inventory': [{'path': 'x'}],
                 'location_dispositions': [{'operation_ids': ['a'], 'note': 'descriptive'}]}
        self.assertEqual(validate_draft(draft, 'notes.md'), draft['operations'])
        self.assertRefused('draft_document_mismatch', validate_draft,
                           dict(draft, document='other.md'), 'notes.md')
        self.assertRefused('unknown_draft_field', validate_draft,
                           dict(draft, extra_key=True), 'notes.md')
        self.assertRefused('invalid_self_check', validate_draft,
                           dict(draft, self_check=['not', 'an', 'object']), 'notes.md')
        self.assertRefused('invalid_location_disposition_reference', validate_draft,
                           dict(draft, location_dispositions=[{'operation_ids': ['zzz']}]),
                           'notes.md')
        self.assertRefused('invalid_location_dispositions', validate_draft,
                           dict(draft, location_dispositions=['nope']), 'notes.md')
        self.assertRefused('invalid_annotation', validate_draft,
                           dict(draft, operations=[{'id': 'a', 'anchor': 'x',
                                                    'replacement': 'y', 'rationale': 5}]),
                           'notes.md')
        self.assertRefused('invalid_anchor', validate_draft,
                           dict(draft, operations=[{'id': 'a', 'anchor': '',
                                                    'replacement': 'y'}]), 'notes.md')

    def test_operation_count_bound(self):
        operations = [{'id': 'o%d' % index, 'anchor': 'x', 'replacement': 'y'}
                      for index in range(4097)]
        draft = {'changeset_format': 'anchored-text-v1', 'document': 'notes.md',
                 'request_id': 'q', 'operations': operations}
        self.assertRefused('too_many_operations', validate_draft, draft, 'notes.md')


class ProposedBoundTests(BoundedCase):
    def test_proposed_size_cap_refuses(self):
        big = 'x' * (MAX_FILE_BYTES + 1)
        op = {'id': 'b', 'op': 'append_items', 'path': ['tail'], 'old_length': 0, 'items': [big]}
        self.assertRefused('proposed_too_large', simulate, 'json-ops-v1', JSON_TAIL, [op], LF0)

    def test_proposed_depth_cap_refuses(self):
        def nested(levels):
            value = 'leaf'
            for _ in range(levels):
                value = [value]
            return value

        deep63 = {'id': 'd', 'op': 'append_items', 'path': ['tail'], 'old_length': 0,
                  'items': [nested(63)]}
        self.assertRefused('proposed_too_deep', simulate, 'json-ops-v1', JSON_TAIL,
                           [deep63], LF0)
        deep64 = {'id': 'd', 'op': 'append_items', 'path': ['tail'], 'old_length': 0,
                  'items': [nested(64)]}
        self.assertRefused('operation_value_too_deep', simulate, 'json-ops-v1', JSON_TAIL,
                           [deep64], LF0)
        deep62 = {'id': 'd', 'op': 'append_items', 'path': ['tail'], 'old_length': 0,
                  'items': [nested(62)]}
        result = simulate('json-ops-v1', JSON_TAIL, [deep62], LF0)
        self.assertTrue(result.startswith(b'{\n  "tail": [\n'))


if __name__ == '__main__':
    unittest.main()
