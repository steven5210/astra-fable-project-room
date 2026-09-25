"""Plan construction: closed envelope -> validated private content-addressed plan.

Exact artifact names written under the state root (interface with the journal):
    plans/<plan_sha256>.json      canonical plan record (hash-bound, no floats)
    templates/<plan_sha256>.json  receipt template (receipt_version 1, unknown)
    staged/<proposed_sha256>      exact proposed output bytes
    drafts/<draft_sha256>         exact validated draft bytes (covers API drafts)
    sources/<source_sha256>       exact pinned source bytes
"""

import hashlib
import os
import stat

from changeset_core import *
from changeset_fs import *
from changeset_fs import _sig
from changeset_ops import validate_draft, simulate
from changeset_receipts import staged_set_digest, template_for
from changeset_faults import fault

ENVELOPE_TOP = {'envelope_version', 'batch_id', 'execution', 'targets', 'conditions'}
TARGET_TOP = {'target', 'source', 'draft', 'renderer'}
SOURCE_TOP = {'bytes', 'sha256', 'mode'}
DRAFT_TOP = {'locator', 'bytes', 'sha256'}
COND_TOP = {'id', 'phase', 'text', 'description', 'inputs', 'why_not_before', 'failure_handling'}
INPUT_TOP = {'id', 'root', 'path', 'bytes', 'sha256'}
PLAN_VERSION = 1
STAGED_DIR = 'staged'
DRAFT_BLOB_DIR = 'drafts'
SOURCE_BLOB_DIR = 'sources'
BLOB_DIRS = (STAGED_DIR, DRAFT_BLOB_DIR, SOURCE_BLOB_DIR)


def _int_field(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        refuse('invalid_' + name)
    return value


def _sha_field(value, name='sha256'):
    if not isinstance(value, str) or len(value) != 64:
        refuse('invalid_' + name)
    for ch in value:
        if ch not in '0123456789abcdef':
            refuse('invalid_' + name)
    return value


def _blob_dir(state, name):
    if name not in BLOB_DIRS:
        refuse('invalid_blob_store')
    try:
        parent = descend(state, ())
    except OSError:
        refuse('state_directory_unavailable')
    try:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
        except OSError:
            refuse('state_directory_unavailable')
        try:
            fd = os.open(name, O_DIR, dir_fd=parent)
        except OSError:
            refuse('state_directory_unavailable')
        try:
            info = os.fstat(fd)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() \
                    or info.st_mode & 0o077:
                refuse('state_directory_unsafe')
        except ChangesetError:
            os.close(fd)
            raise
        return fd
    finally:
        os.close(parent)


def _stream_digest(root, parts, maximum=MAX_FILE_BYTES):
    try:
        dir_fd = descend(root, parts[:-1])
    except FileNotFoundError:
        refuse('input_missing')
    except OSError:
        refuse('input_unreadable')
    try:
        fd, before = open_regular_at(dir_fd, parts[-1])
    finally:
        os.close(dir_fd)
    digest = hashlib.sha256()
    total = 0
    try:
        if before.st_size > maximum:
            refuse('file_too_large')
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > maximum:
                refuse('file_too_large')
            digest.update(chunk)
        after = os.fstat(fd)
        if _sig(before) != _sig(after):
            refuse('changed_while_read')
    finally:
        os.close(fd)
    return total, digest.hexdigest(), after


def _read_draft(input_root, locator, supplied_drafts):
    parts = normalize_rel(locator)
    supplied = None
    if supplied_drafts is not None and locator in supplied_drafts:
        supplied = supplied_drafts[locator]
        if not isinstance(supplied, bytes):
            refuse('invalid_supplied_draft')
        if len(supplied) > MAX_FILE_BYTES:
            refuse('file_too_large')
    onfile = None
    try:
        dir_fd = descend(input_root, parts[:-1])
    except FileNotFoundError:
        dir_fd = None
    except OSError:
        refuse('draft_unreadable')
    if dir_fd is not None:
        try:
            onfile, _ = read_file_at(dir_fd, parts[-1], MAX_FILE_BYTES)
        except ChangesetError as exc:
            if exc.code != 'file_missing':
                raise
        finally:
            os.close(dir_fd)
    if supplied is not None and onfile is not None and supplied != onfile:
        refuse('draft_locator_conflict')
    data = supplied if supplied is not None else onfile
    if data is None:
        refuse('draft_missing')
    return data


def build_plan(workspace, state, input_root, envelope_bytes, supplied_drafts=None):
    fault('plan_begin')
    require_platform_support(state)
    if supplied_drafts is not None and not isinstance(supplied_drafts, dict):
        refuse('invalid_supplied_drafts')
    env = strict_loads(envelope_bytes, limit=MAX_FILE_BYTES)
    if not isinstance(env, dict) or set(env) != ENVELOPE_TOP:
        refuse('invalid_envelope')
    if type(env['envelope_version']) is not int or env['envelope_version'] != 1:
        refuse('invalid_envelope_version')
    check_id(env['batch_id'], 'batch_id')
    if env['execution'] != 'sequential-v1':
        refuse('invalid_execution')
    targets = env['targets']
    if not isinstance(targets, list) or not targets or len(targets) > MAX_TARGETS:
        refuse('invalid_targets')
    conditions = env['conditions']
    if not isinstance(conditions, list) or len(conditions) > MAX_CONDITIONS:
        refuse('invalid_conditions')

    plan_targets = []
    seen_paths = set()
    seen_inodes = set()
    target_identities = set()
    all_ids = []
    retained = 0
    total_ops = 0
    fds = []
    try:
        for name in BLOB_DIRS:
            fds.append(_blob_dir(state, name))
        staged_fd, drafts_fd, sources_fd = fds
        for entry in targets:
            if not isinstance(entry, dict) or set(entry) != TARGET_TOP:
                refuse('invalid_target_entry')
            target = entry['target']
            parts = normalize_rel(target)
            if target in seen_paths:
                refuse('duplicate_target')
            seen_paths.add(target)
            if not isinstance(entry['source'], dict) or set(entry['source']) != SOURCE_TOP:
                refuse('invalid_source')
            source = entry['source']
            source_bytes = _int_field(source['bytes'], 'source_bytes')
            if source_bytes > MAX_FILE_BYTES:
                refuse('file_too_large')
            source_sha = _sha_field(source['sha256'])
            source_mode = _int_field(source['mode'], 'source_mode')
            if source_mode & 0o7000 or source_mode > 0o777:
                refuse('special_mode_bits')
            try:
                dir_fd = descend(workspace, parts[:-1])
            except FileNotFoundError:
                refuse('file_missing')
            except OSError:
                refuse('target_unreadable')
            try:
                fd, st = open_regular_at(dir_fd, parts[-1])
            finally:
                os.close(dir_fd)
            chunks = []
            total = 0
            try:
                if st.st_size > MAX_FILE_BYTES:
                    refuse('file_too_large')
                while True:
                    chunk = os.read(fd, 65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_FILE_BYTES:
                        refuse('file_too_large')
                    chunks.append(chunk)
                after = os.fstat(fd)
                if _sig(st) != _sig(after):
                    refuse('changed_while_read')
                meta = snapshot_meta(fd)
                final = os.fstat(fd)
                if _sig(after) != _sig(final):
                    refuse('changed_while_read')
            finally:
                os.close(fd)
            data = b''.join(chunks)
            if len(data) != source_bytes or sha256(data) != source_sha:
                refuse('source_pin_mismatch')
            if stat.S_IMODE(final.st_mode) != source_mode:
                refuse('source_mode_mismatch')
            ident = (final.st_dev, final.st_ino)
            if ident in seen_inodes:
                refuse('duplicate_target_inode')
            seen_inodes.add(ident)
            target_identities.add(ident)
            if not isinstance(entry['draft'], dict) or set(entry['draft']) != DRAFT_TOP:
                refuse('invalid_draft_entry')
            draft_decl = entry['draft']
            draft_bytes_n = _int_field(draft_decl['bytes'], 'draft_bytes')
            if draft_bytes_n > MAX_FILE_BYTES:
                refuse('file_too_large')
            draft_sha = _sha_field(draft_decl['sha256'])
            locator = draft_decl['locator']
            normalize_rel(locator)
            raw = _read_draft(input_root, locator, supplied_drafts)
            if len(raw) != draft_bytes_n or sha256(raw) != draft_sha:
                refuse('draft_pin_mismatch')
            draft = strict_loads(raw, limit=MAX_FILE_BYTES)
            operations = validate_draft(draft, target)
            total_ops += len(operations)
            if total_ops > MAX_OPERATIONS:
                refuse('too_many_operations')
            fmt = draft['changeset_format']
            renderer = entry['renderer']
            if fmt == 'json-ops-v1':
                if renderer not in RENDERERS:
                    refuse('invalid_renderer')
            elif renderer is not None:
                refuse('renderer_not_applicable')
            proposed = simulate(fmt, data, operations, renderer)
            if len(proposed) > MAX_FILE_BYTES:
                refuse('proposed_too_large')
            proposed_sha = sha256(proposed)
            xattr_map = [{'name': b64e(name), 'value': b64e(value)} for name, value in meta.xattrs]
            xattr_bytes = sum(len(name) + len(value) for name, value in meta.xattrs)
            retained += len(data) + len(raw) + len(proposed) + xattr_bytes
            if retained > MAX_AGGREGATE_BYTES:
                refuse('aggregate_bytes_exceeded')
            write_content_addressed(staged_fd, proposed_sha, proposed)
            write_content_addressed(drafts_fd, draft_sha, raw)
            write_content_addressed(sources_fd, source_sha, data)
            plan_targets.append({
                'target': target,
                'source': {
                    'bytes': source_bytes, 'sha256': source_sha, 'mode': source_mode,
                    'device': final.st_dev, 'inode': final.st_ino,
                    'metadata_digest': meta_digest(meta),
                    'metadata': meta_to_record(meta),
                    'xattr_count': len(meta.xattrs), 'xattr_bytes': xattr_bytes,
                    'xattrs': xattr_map, 'flags': meta.flags, 'acl': meta.acl,
                },
                'draft': {
                    'locator': locator, 'bytes': draft_bytes_n, 'sha256': draft_sha,
                    'origin': 'inline' if supplied_drafts is not None and locator in supplied_drafts else 'input',
                    'blob': DRAFT_BLOB_DIR + '/' + draft_sha,
                },
                'blobs': {
                    'source': SOURCE_BLOB_DIR + '/' + source_sha,
                    'draft': DRAFT_BLOB_DIR + '/' + draft_sha,
                    'staged': STAGED_DIR + '/' + proposed_sha,
                },
                'renderer': renderer,
                'changeset_format': fmt,
                'request_id': draft['request_id'],
                'operation_ids': [op['id'] for op in operations],
                'proposed': {'bytes': len(proposed), 'sha256': proposed_sha},
                'metadata': {'xattr_count': len(meta.xattrs), 'xattr_bytes': xattr_bytes,
                             'flags': meta.flags, 'acl': meta.acl},
            })
            all_ids.extend(op['id'] for op in operations)
    finally:
        for fd in fds:
            os.close(fd)

    if len(set(all_ids)) != len(all_ids):
        refuse('duplicate_operation_id')

    pinned = []
    plan_conditions = []
    cond_ids = set()
    input_ids = set()
    for cond in conditions:
        if not isinstance(cond, dict) or set(cond) - COND_TOP or not {'id', 'phase', 'text', 'description', 'inputs'} <= set(cond):
            refuse('invalid_condition')
        cond_id = check_id(cond['id'], 'condition_id')
        if cond_id in cond_ids:
            refuse('duplicate_condition_id')
        cond_ids.add(cond_id)
        phase = cond['phase']
        if phase not in ('pre_apply', 'post_apply'):
            refuse('invalid_condition_phase')
        if not isinstance(cond['text'], str) or not cond['text'] \
                or not isinstance(cond['description'], str) or not cond['description']:
            refuse('invalid_condition_text')
        if phase == 'post_apply':
            if not isinstance(cond.get('why_not_before'), str) or not cond['why_not_before']:
                refuse('missing_post_apply_explanation')
            if not isinstance(cond.get('failure_handling'), str) or not cond['failure_handling']:
                refuse('missing_post_apply_failure_handling')
        else:
            if 'why_not_before' in cond or 'failure_handling' in cond:
                refuse('invalid_pre_apply_condition')
        inputs = cond['inputs']
        if not isinstance(inputs, list):
            refuse('invalid_condition_inputs')
        inputs_out = []
        for item in inputs:
            if not isinstance(item, dict) or set(item) != INPUT_TOP:
                refuse('invalid_condition_input')
            input_id = check_id(item['id'], 'input_id')
            if input_id in input_ids:
                refuse('duplicate_input_id')
            input_ids.add(input_id)
            if item['root'] not in ('workspace', 'input'):
                refuse('invalid_input_root')
            iparts = normalize_rel(item['path'])
            input_bytes = _int_field(item['bytes'], 'input_bytes')
            if input_bytes > MAX_FILE_BYTES:
                refuse('file_too_large')
            input_sha = _sha_field(item['sha256'])
            if len(pinned) >= MAX_PINNED_INPUTS:
                refuse('too_many_pinned_inputs')
            root = workspace if item['root'] == 'workspace' else input_root
            seen_bytes, seen_sha, seen_stat = _stream_digest(root, iparts)
            if seen_bytes != input_bytes or seen_sha != input_sha:
                refuse('input_pin_mismatch')
            if (seen_stat.st_dev, seen_stat.st_ino) in target_identities:
                refuse('input_aliases_target')
            pinned.append({'condition_id': cond_id, 'input_id': input_id, 'root': item['root'],
                           'path': item['path'], 'bytes': input_bytes, 'sha256': input_sha})
            inputs_out.append({'id': input_id, 'root': item['root'], 'path': item['path'],
                               'bytes': input_bytes, 'sha256': input_sha})
        plan_conditions.append({'id': cond_id, 'phase': phase, 'text': cond['text'],
                                'description': cond['description'], 'inputs': inputs_out,
                                'why_not_before': cond.get('why_not_before'),
                                'failure_handling': cond.get('failure_handling')})

    plan = {
        'plan_version': PLAN_VERSION,
        'batch_id': env['batch_id'],
        'execution': 'sequential-v1',
        'workspace': {'path': workspace.path, 'device': workspace.dev,
                      'inode': workspace.ino, 'mode': workspace.mode},
        'input_root': {'path': input_root.path, 'device': input_root.dev,
                       'inode': input_root.ino, 'mode': input_root.mode},
        'state_root': {'path': state.path, 'device': state.dev, 'inode': state.ino, 'mode': state.mode},
        'stores': {'staged': STAGED_DIR, 'drafts': DRAFT_BLOB_DIR, 'sources': SOURCE_BLOB_DIR},
        'targets': plan_targets,
        'conditions': plan_conditions,
        'pinned_inputs': pinned,
    }
    plan['plan_sha256'] = digest_object(plan, 'plan_sha256')
    validate_saved_plan(plan)
    _write_plan_files(state, plan)
    return plan


def _write_plan_files(state, plan):
    plan_sha = plan['plan_sha256']
    data = canonical(plan).encode('utf-8')
    if len(data) > MAX_FILE_BYTES:
        refuse('plan_too_large')
    template_data = canonical(template_for(plan)).encode('utf-8')
    if len(template_data) > MAX_FILE_BYTES:
        refuse('template_too_large')
    plans_fd = None
    templates_fd = None
    try:
        try:
            plans_fd = descend(state, ('plans',))
            templates_fd = descend(state, ('templates',))
        except OSError:
            refuse('state_directory_unavailable')
        write_new_at(plans_fd, plan_sha + '.json', data)
        write_new_at(templates_fd, plan_sha + '.json', template_data)
    finally:
        if plans_fd is not None:
            os.close(plans_fd)
        if templates_fd is not None:
            os.close(templates_fd)


def validate_saved_plan(plan):
    """Validate persisted structure before the journal can interpret any field.

    A matching digest is not a schema validator. Derived fields must agree with
    their source inventory, and private paths never choose a different store.
    """
    def keys(value, expected):
        if not isinstance(value, dict) or set(value) != set(expected):
            refuse('plan_schema_invalid')

    def count(value, maximum):
        if type(value) is not int or not 0 <= value <= maximum:
            refuse('plan_schema_invalid')

    def root_record(value):
        keys(value, ('path', 'device', 'inode', 'mode'))
        path = value['path']
        if (not isinstance(path, str) or not path.startswith('/') or '\0' in path
                or '\\' in path or path != os.path.normpath(path)
                or (path != '/' and any(p in ('', '.', '..') for p in path[1:].split('/')))
                or len(path[1:].split('/')) > MAX_PATH_DEPTH):
            refuse('plan_schema_invalid')
        count(value['device'], (1 << 64) - 1)
        count(value['inode'], (1 << 64) - 1)
        count(value['mode'], 0o777)

    keys(plan, ('plan_version', 'batch_id', 'execution', 'workspace', 'input_root',
                'state_root', 'stores', 'targets', 'conditions', 'pinned_inputs', 'plan_sha256'))
    if type(plan['plan_version']) is not int or plan['plan_version'] != PLAN_VERSION:
        refuse('plan_schema_invalid')
    check_id(plan['batch_id'], 'batch_id')
    if plan['execution'] != 'sequential-v1':
        refuse('plan_schema_invalid')
    for name in ('workspace', 'input_root', 'state_root'):
        root_record(plan[name])
    if plan['stores'] != {'staged': STAGED_DIR, 'drafts': DRAFT_BLOB_DIR, 'sources': SOURCE_BLOB_DIR}:
        refuse('plan_schema_invalid')
    targets = plan['targets']
    if not isinstance(targets, list) or not 1 <= len(targets) <= MAX_TARGETS:
        refuse('plan_schema_invalid')
    paths, inodes, operations = set(), set(), set()
    retained = 0
    for target in targets:
        keys(target, ('target', 'source', 'draft', 'blobs', 'renderer', 'changeset_format',
                      'request_id', 'operation_ids', 'proposed', 'metadata'))
        normalize_rel(target['target'])
        if target['target'] in paths:
            refuse('plan_schema_invalid')
        paths.add(target['target'])
        source, draft, proposed = target['source'], target['draft'], target['proposed']
        keys(source, ('bytes', 'sha256', 'mode', 'device', 'inode', 'metadata_digest',
                      'metadata', 'xattr_count', 'xattr_bytes', 'xattrs', 'flags', 'acl'))
        keys(draft, ('locator', 'bytes', 'sha256', 'origin', 'blob'))
        keys(proposed, ('bytes', 'sha256'))
        for value in (source, draft, proposed):
            count(value['bytes'], MAX_FILE_BYTES)
            _sha_field(value['sha256'])
        count(source['device'], (1 << 64) - 1)
        count(source['inode'], (1 << 64) - 1)
        identity = (source['device'], source['inode'])
        if identity in inodes:
            refuse('plan_schema_invalid')
        inodes.add(identity)
        metadata = meta_from_record(source['metadata'])
        expected_attrs = meta_to_record(metadata)['xattrs']
        attr_bytes = sum(len(name) + len(value) for name, value in metadata.xattrs)
        count(source['mode'], 0o777)
        count(source['xattr_count'], MAX_XATTRS)
        count(source['xattr_bytes'], MAX_XATTR_BYTES)
        if (source['mode'] != metadata.mode or source['flags'] != metadata.flags
                or type(source['flags']) is not int or source['acl'] != metadata.acl
                or source['metadata_digest'] != meta_digest(metadata)
                or source['xattrs'] != expected_attrs
                or source['xattr_count'] != len(metadata.xattrs) or source['xattr_bytes'] != attr_bytes):
            refuse('plan_schema_invalid')
        if not typed_equal(target['metadata'], {'xattr_count': len(metadata.xattrs), 'xattr_bytes': attr_bytes,
                                               'flags': metadata.flags, 'acl': metadata.acl}):
            refuse('plan_schema_invalid')
        normalize_rel(draft['locator'])
        if draft['origin'] not in ('input', 'inline') or draft['blob'] != DRAFT_BLOB_DIR + '/' + draft['sha256']:
            refuse('plan_schema_invalid')
        if target['blobs'] != {'source': SOURCE_BLOB_DIR + '/' + source['sha256'],
                               'draft': DRAFT_BLOB_DIR + '/' + draft['sha256'],
                               'staged': STAGED_DIR + '/' + proposed['sha256']}:
            refuse('plan_schema_invalid')
        if ((target['changeset_format'] == 'json-ops-v1' and target['renderer'] not in RENDERERS)
                or (target['changeset_format'] == 'anchored-text-v1' and target['renderer'] is not None)
                or target['changeset_format'] not in ('json-ops-v1', 'anchored-text-v1')):
            refuse('plan_schema_invalid')
        check_id(target['request_id'], 'request_id')
        ids = target['operation_ids']
        if not isinstance(ids, list) or len(ids) > MAX_OPERATIONS:
            refuse('plan_schema_invalid')
        for identifier in ids:
            check_id(identifier, 'operation_id')
            if identifier in operations:
                refuse('plan_schema_invalid')
            operations.add(identifier)
        if len(operations) > MAX_OPERATIONS:
            refuse('plan_schema_invalid')
        retained += source['bytes'] + draft['bytes'] + proposed['bytes'] + attr_bytes
        if retained > MAX_AGGREGATE_BYTES:
            refuse('plan_schema_invalid')
    conditions = plan['conditions']
    if not isinstance(conditions, list) or len(conditions) > MAX_CONDITIONS:
        refuse('plan_schema_invalid')
    condition_ids, input_ids, pinned = set(), set(), []
    for condition in conditions:
        keys(condition, ('id', 'phase', 'text', 'description', 'inputs', 'why_not_before', 'failure_handling'))
        identifier = check_id(condition['id'], 'condition_id')
        if identifier in condition_ids:
            refuse('plan_schema_invalid')
        condition_ids.add(identifier)
        if any(not isinstance(condition[k], str) or not condition[k] for k in ('text', 'description')):
            refuse('plan_schema_invalid')
        if condition['phase'] == 'pre_apply':
            if condition['why_not_before'] is not None or condition['failure_handling'] is not None:
                refuse('plan_schema_invalid')
        elif condition['phase'] == 'post_apply':
            if any(not isinstance(condition[k], str) or not condition[k] for k in ('why_not_before', 'failure_handling')):
                refuse('plan_schema_invalid')
        else:
            refuse('plan_schema_invalid')
        if not isinstance(condition['inputs'], list) or len(condition['inputs']) > MAX_PINNED_INPUTS:
            refuse('plan_schema_invalid')
        for item in condition['inputs']:
            keys(item, INPUT_TOP)
            input_id = check_id(item['id'], 'input_id')
            if input_id in input_ids or item['root'] not in ('workspace', 'input'):
                refuse('plan_schema_invalid')
            input_ids.add(input_id)
            normalize_rel(item['path'])
            count(item['bytes'], MAX_FILE_BYTES)
            _sha_field(item['sha256'])
            pinned.append({'condition_id': identifier, 'input_id': input_id, 'root': item['root'],
                           'path': item['path'], 'bytes': item['bytes'], 'sha256': item['sha256']})
            if len(pinned) > MAX_PINNED_INPUTS:
                refuse('plan_schema_invalid')
    if not isinstance(plan['pinned_inputs'], list) or not typed_equal(plan['pinned_inputs'], pinned):
        refuse('plan_schema_invalid')
    _sha_field(plan['plan_sha256'])
    if digest_object(plan, 'plan_sha256') != plan['plan_sha256']:
        refuse('plan_tampered')


def load_plan(state, plan_sha):
    if not isinstance(plan_sha, str) or len(plan_sha) != 64 \
            or any(ch not in '0123456789abcdef' for ch in plan_sha):
        refuse('invalid_plan_id')
    try:
        plan_fd = descend(state, ('plans',))
    except OSError:
        refuse('plan_missing')
    try:
        try:
            data, _ = read_file_at(plan_fd, plan_sha + '.json', MAX_FILE_BYTES)
        except ChangesetError as exc:
            if exc.code == 'file_missing':
                refuse('plan_missing')
            raise
    finally:
        os.close(plan_fd)
    plan = strict_loads(data, limit=MAX_FILE_BYTES)
    validate_saved_plan(plan)
    if plan.get('plan_sha256') != plan_sha or digest_object(plan, 'plan_sha256') != plan_sha:
        refuse('plan_tampered')
    return plan


def plan_summary(plan, state):
    pre = sum(1 for condition in plan['conditions'] if condition['phase'] == 'pre_apply')
    post = sum(1 for condition in plan['conditions'] if condition['phase'] == 'post_apply')
    acl_states = {target['metadata']['acl'] for target in plan['targets']}
    return {
        'tool': 'changeset_tool',
        'version': 'changeset-tool-v1',
        'outcome': 'planned',
        'plan_sha256': plan['plan_sha256'],
        'batch_id_sha256': id_digest(plan['batch_id']),
        'counts': {
            'targets': len(plan['targets']),
            'operations': sum(len(target['operation_ids']) for target in plan['targets']),
            'conditions': {'pre_apply': pre, 'post_apply': post},
            'pinned_inputs': len(plan['pinned_inputs']),
        },
        'coverage': {
            'xattr': 'complete',
            'acl': 'verified' if acl_states <= {'absent', 'empty'} else 'unknown',
            'flags': 'verified',
        },
        'evidence': {
            'plan_ref': 'plans/' + plan['plan_sha256'] + '.json',
            'template_ref': 'templates/' + plan['plan_sha256'] + '.json',
            'staged_set_sha256': staged_set_digest(plan),
            'stores': dict(plan['stores']),
        },
    }
