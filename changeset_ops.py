from changeset_core import *

ANNOTATIONS = {'location', 'rationale', 'requirement'}
COMMON = {'id'}
JSON_FIELDS = {
    'set_value': {'op', 'path', 'old', 'new'},
    'edit_string': {'op', 'path', 'anchor', 'replacement'},
    'replace_string': {'op', 'path', 'old_starts_with', 'old_ends_with', 'new'},
    'append_items': {'op', 'path', 'old_length', 'items'},
    'insert_key_after': {'op', 'path', 'after_key', 'key', 'value'},
    'insert_items_after': {'op', 'path', 'after', 'items'},
}
DRAFT_TOP = {'changeset_format', 'document', 'request_id', 'operations',
             'self_check', 'inventory', 'location_dispositions'}
PAYLOAD_KEYS = ('old', 'new', 'value', 'items')


def check_path(path):
    if not isinstance(path, list):
        refuse('invalid_path')
    if len(path) > MAX_PATH_DEPTH:
        refuse('path_too_deep')
    for element in path:
        if isinstance(element, bool):
            refuse('invalid_path_element')
        if isinstance(element, str):
            continue
        if type(element) is int:
            if element < 0:
                refuse('invalid_path_index')
            continue
        if isinstance(element, dict):
            if len(element) != 1 or 'id' not in element or not isinstance(element['id'], str):
                refuse('invalid_selector')
            continue
        refuse('invalid_path_element')


def _check_annotations(op):
    for key in ANNOTATIONS:
        if key in op and not isinstance(op[key], str):
            refuse('invalid_annotation', operation_id=op.get('id'))


def _require_fields(op, required, op_id):
    if not required <= set(op):
        refuse('missing_operation_field', operation_id=op_id)


def _check_payload_depth(op):
    for key in PAYLOAD_KEYS:
        if key in op and not value_depth_ok(op[key], MAX_JSON_DEPTH):
            refuse('operation_value_too_deep', operation_id=op.get('id'))


def validate_operation(op, fmt, op_id=None):
    if not isinstance(op, dict):
        refuse('invalid_operation', operation_id=op_id)
    if 'id' not in op:
        refuse('missing_operation_id', operation_id=op_id)
    check_id(op['id'], 'operation_id')
    if fmt == 'anchored-text-v1':
        allowed = COMMON | ANNOTATIONS | {'anchor', 'replacement'}
        if set(op) - allowed:
            refuse('unknown_operation_field', operation_id=op['id'])
        _require_fields(op, COMMON | {'anchor', 'replacement'}, op['id'])
        if not isinstance(op['anchor'], str) or not op['anchor']:
            refuse('invalid_anchor', operation_id=op['id'])
        if not isinstance(op['replacement'], str):
            refuse('invalid_replacement', operation_id=op['id'])
        _check_annotations(op)
        return
    if fmt != 'json-ops-v1':
        refuse('invalid_changeset_format')
    if not isinstance(op.get('op'), str) or op['op'] not in JSON_FIELDS:
        refuse('unknown_operation', operation_id=op['id'])
    name = op['op']
    if set(op) - (COMMON | ANNOTATIONS | JSON_FIELDS[name]):
        refuse('unknown_operation_field', operation_id=op['id'])
    _require_fields(op, COMMON | JSON_FIELDS[name], op['id'])
    check_path(op['path'])
    if name in ('set_value', 'edit_string', 'replace_string') and not op['path']:
        refuse('empty_path', operation_id=op['id'])
    if name == 'edit_string':
        if not isinstance(op['anchor'], str) or not op['anchor']:
            refuse('invalid_anchor', operation_id=op['id'])
        if not isinstance(op['replacement'], str):
            refuse('invalid_replacement', operation_id=op['id'])
    elif name == 'replace_string':
        for key in ('old_starts_with', 'old_ends_with'):
            if not isinstance(op[key], str) or not op[key]:
                refuse('invalid_boundary', operation_id=op['id'])
        if not isinstance(op['new'], str):
            refuse('invalid_new', operation_id=op['id'])
    elif name == 'append_items':
        if type(op['old_length']) is not int or op['old_length'] < 0:
            refuse('invalid_old_length', operation_id=op['id'])
        if not isinstance(op['items'], list):
            refuse('invalid_items', operation_id=op['id'])
    elif name == 'insert_key_after':
        if not isinstance(op['after_key'], str) or not isinstance(op['key'], str):
            refuse('invalid_key', operation_id=op['id'])
    elif name == 'insert_items_after':
        if not isinstance(op['after'], dict) or len(op['after']) != 1 \
                or 'id' not in op['after'] or not isinstance(op['after']['id'], str):
            refuse('invalid_selector', operation_id=op['id'])
        if not isinstance(op['items'], list):
            refuse('invalid_items', operation_id=op['id'])
    _check_payload_depth(op)
    _check_annotations(op)


def validate_draft(draft, target_document):
    if not isinstance(draft, dict):
        refuse('invalid_draft')
    if set(draft) - DRAFT_TOP:
        refuse('unknown_draft_field')
    fmt = draft.get('changeset_format')
    if fmt not in ('json-ops-v1', 'anchored-text-v1'):
        refuse('invalid_changeset_format')
    if draft.get('document') != target_document:
        refuse('draft_document_mismatch')
    check_id(draft.get('request_id'), 'request_id')
    operations = draft.get('operations')
    if not isinstance(operations, list):
        refuse('invalid_operations')
    if len(operations) > MAX_OPERATIONS:
        refuse('too_many_operations')
    if 'self_check' in draft and not isinstance(draft['self_check'], dict):
        refuse('invalid_self_check')
    if 'inventory' in draft and not isinstance(draft['inventory'], list):
        refuse('invalid_inventory')
    ids = []
    for op in operations:
        validate_operation(op, fmt)
        ids.append(op['id'])
    if len(set(ids)) != len(ids):
        refuse('duplicate_operation_id')
    if 'location_dispositions' in draft:
        value = draft['location_dispositions']
        if not isinstance(value, list):
            refuse('invalid_location_dispositions')
        known = set(ids)
        for item in value:
            if not isinstance(item, dict) or not isinstance(item.get('operation_ids'), list):
                refuse('invalid_location_dispositions')
            for ref in item['operation_ids']:
                if not isinstance(ref, str) or ref not in known:
                    refuse('invalid_location_disposition_reference')
    return operations


def _copy_json(value):
    if isinstance(value, list):
        root = []
    elif isinstance(value, dict):
        root = {}
    else:
        return value
    stack = [(value, root)]
    while stack:
        source, target = stack.pop()
        if isinstance(source, dict):
            for key, item in source.items():
                if isinstance(item, list):
                    child = []
                    stack.append((item, child))
                elif isinstance(item, dict):
                    child = {}
                    stack.append((item, child))
                else:
                    child = item
                target[key] = child
        else:
            for item in source:
                if isinstance(item, list):
                    child = []
                    stack.append((item, child))
                elif isinstance(item, dict):
                    child = {}
                    stack.append((item, child))
                else:
                    child = item
                target.append(child)
    return root


def _selector_index(array, selector):
    if not isinstance(array, list):
        refuse('path_not_array')
    matches = []
    for index, item in enumerate(array):
        if isinstance(item, dict) and isinstance(item.get('id'), str) and item['id'] == selector['id']:
            matches.append(index)
    if len(matches) != 1:
        refuse('ambiguous_selector')
    return matches[0]


def _get(doc, path):
    current = doc
    for element in path:
        if isinstance(element, str):
            if not isinstance(current, dict) or element not in current:
                refuse('path_missing')
            current = current[element]
        elif type(element) is int:
            if not isinstance(current, list) or element < 0 or element >= len(current):
                refuse('path_missing')
            current = current[element]
        elif isinstance(element, dict):
            if not isinstance(current, list):
                refuse('path_missing')
            current = current[_selector_index(current, element)]
        else:
            refuse('invalid_path_element')
    return current


def _set(doc, path, value):
    if not path:
        return value
    parent = _get(doc, path[:-1])
    last = path[-1]
    if isinstance(last, str):
        if not isinstance(parent, dict) or last not in parent:
            refuse('path_missing')
        parent[last] = value
    elif type(last) is int:
        if not isinstance(parent, list) or last < 0 or last >= len(parent):
            refuse('path_missing')
        parent[last] = value
    elif isinstance(last, dict):
        parent[_selector_index(parent, last)] = value
    else:
        refuse('invalid_path_element')
    return doc


def _count_ids(value):
    if not isinstance(value, list):
        return None
    counts = {}
    for item in value:
        if isinstance(item, dict) and isinstance(item.get('id'), str):
            counts[item['id']] = counts.get(item['id'], 0) + 1
    return counts


def _fetch(doc, path):
    try:
        return _get(doc, path)
    except ChangesetError:
        return None


def _affected_array_paths(op):
    name = op.get('op')
    path = op.get('path')
    if not isinstance(path, list):
        return ()
    if name in ('append_items', 'insert_items_after'):
        return (path,)
    if name == 'insert_key_after':
        if op.get('key') == 'id' and path and (type(path[-1]) is int or isinstance(path[-1], dict)):
            return (path[:-1],)
        return ()
    if name in ('set_value', 'edit_string', 'replace_string'):
        paths = []
        if path:
            last = path[-1]
            if type(last) is int or isinstance(last, dict):
                paths.append(path[:-1])
            elif last == 'id' and len(path) >= 2:
                previous = path[-2]
                if type(previous) is int or isinstance(previous, dict):
                    paths.append(path[:-2])
        if name == 'set_value' and path and isinstance(op.get('new'), list):
            paths.append(path)
        return tuple(paths)
    return ()


def affected_array_path(op):
    paths = _affected_array_paths(op)
    return paths[0] if paths else None


def _check_duplicate_ids(doc, op, checks):
    for path, before in checks:
        after = _count_ids(_fetch(doc, path))
        if after is None:
            continue
        before = before or {}
        for key, count in after.items():
            if count > 1 and count > before.get(key, 0):
                refuse('duplicate_id_introduced', operation_id=op.get('id'))


def _count_anchor(text, anchor):
    count = 0
    start = 0
    while True:
        index = text.find(anchor, start)
        if index < 0:
            break
        count += 1
        start = index + 1
    return count


def _edit_anchor(text, anchor, replacement, op_id):
    if _count_anchor(text, anchor) != 1:
        refuse('anchor_not_unique', operation_id=op_id)
    index = text.find(anchor)
    return text[:index] + replacement + text[index + len(anchor):]


def apply_json_op(doc, op):
    validate_operation(op, 'json-ops-v1')
    checks = [(path, _count_ids(_fetch(doc, path))) for path in _affected_array_paths(op)]
    name = op['op']
    if name == 'set_value':
        current = _get(doc, op['path'])
        if not typed_equal(current, op['old']):
            refuse('old_value_mismatch', operation_id=op['id'])
        doc = _set(doc, op['path'], _copy_json(op['new']))
    elif name == 'edit_string':
        current = _get(doc, op['path'])
        if not isinstance(current, str):
            refuse('value_not_string', operation_id=op['id'])
        doc = _set(doc, op['path'], _edit_anchor(current, op['anchor'], op['replacement'], op['id']))
    elif name == 'replace_string':
        current = _get(doc, op['path'])
        if not isinstance(current, str):
            refuse('value_not_string', operation_id=op['id'])
        if not current.startswith(op['old_starts_with']) or not current.endswith(op['old_ends_with']):
            refuse('boundary_mismatch', operation_id=op['id'])
        doc = _set(doc, op['path'], op['new'])
    elif name == 'append_items':
        array = _get(doc, op['path'])
        if not isinstance(array, list) or len(array) != op['old_length']:
            refuse('length_mismatch', operation_id=op['id'])
        array.extend(_copy_json(op['items']))
    elif name == 'insert_key_after':
        parent = _get(doc, op['path'])
        if not isinstance(parent, dict) or op['after_key'] not in parent or op['key'] in parent:
            refuse('insert_key_mismatch', operation_id=op['id'])
        replacement = {}
        for key, value in parent.items():
            replacement[key] = value
            if key == op['after_key']:
                replacement[op['key']] = _copy_json(op['value'])
        doc = _set(doc, op['path'], replacement)
    elif name == 'insert_items_after':
        array = _get(doc, op['path'])
        if not isinstance(array, list):
            refuse('path_not_array', operation_id=op['id'])
        index = _selector_index(array, op['after'])
        array[index + 1:index + 1] = _copy_json(op['items'])
    else:
        refuse('unknown_operation', operation_id=op['id'])
    _check_duplicate_ids(doc, op, checks)
    return doc


def simulate(fmt, source_bytes, operations, renderer):
    if not isinstance(source_bytes, bytes):
        refuse('invalid_source_bytes')
    if not isinstance(operations, list):
        refuse('invalid_operations')
    for op in operations:
        validate_operation(op, fmt)
    if fmt == 'json-ops-v1':
        if renderer not in RENDERERS:
            refuse('invalid_renderer')
        doc = strict_loads(source_bytes, limit=MAX_FILE_BYTES)
        if render_json(doc, renderer) != source_bytes:
            refuse('formatting_mismatch')
        for op in operations:
            doc = apply_json_op(doc, op)
        if not value_depth_ok(doc, MAX_JSON_DEPTH):
            refuse('proposed_too_deep')
        proposed = render_json(doc, renderer)
        if len(proposed) > MAX_FILE_BYTES:
            refuse('proposed_too_large')
        return proposed
    if fmt == 'anchored-text-v1':
        if renderer is not None:
            refuse('renderer_not_applicable')
        text = decode_text(source_bytes)
        for op in operations:
            text = _edit_anchor(text, op['anchor'], op['replacement'], op['id'])
        proposed = text.encode('utf-8')
        if len(proposed) > MAX_FILE_BYTES:
            refuse('proposed_too_large')
        return proposed
    refuse('invalid_changeset_format')
