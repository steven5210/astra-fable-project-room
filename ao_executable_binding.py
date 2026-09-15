"""Explicit no-inference repair of a missing/changed native Claude executable.

The original preparation is immutable. A hash-bound journal records replacement
identity separately; every routing check validates it. The operator installs the
binary and changes AO's launch path separately. This module never does either,
never operates a native lifecycle, and never releases a semantic outcome hold.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import time

from ao_delegate_launcher import owned_bytes
from ao_native_identity import read_owner
from ao_reviewer_recovery import _store_once
from room import RoomError

BASE = 'executable-bindings'
MAX_BINARY = 512_000_000


def _fingerprint(path):
    path = Path(path)
    if not path.is_absolute() or path != path.resolve() or any(p.is_symlink() for p in (path, *path.parents)):
        raise RoomError('Replacement executable must have a canonical absolute path without symlinks')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_mode & 0o022 or not before.st_mode & 0o111
                or not 0 < before.st_size <= MAX_BINARY):
            raise RoomError('Replacement executable ownership, permissions or size is unsafe')
        digest = hashlib.sha256()
        total = 0
        while True:
            block = os.read(fd, min(1024 * 1024, MAX_BINARY + 1 - total))
            if not block:
                break
            total += len(block)
            if total > MAX_BINARY:
                raise RoomError("Replacement executable exceeded its bounded read")
            digest.update(block)
        after = os.fstat(fd)
        current = path.lstat()
        keys = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_mode', 'st_uid')
        if any(getattr(before, k) != getattr(s, k) for s in (after, current) for k in keys):
            raise RoomError('Replacement executable changed while being inspected')
        return {'path': str(path), 'size': before.st_size, 'mtime_ns': before.st_mtime_ns,
                'sha256': digest.hexdigest()}
    finally:
        os.close(fd)


def _launch(path, target):
    path = Path(path)
    if not path.is_absolute() or any(p.is_symlink() for p in path.parents):
        raise RoomError('AO launch path must be absolute with no symlinked parent')
    info = path.lstat()
    if info.st_uid != os.getuid() or not (stat.S_ISLNK(info.st_mode) or stat.S_ISREG(info.st_mode)):
        raise RoomError('AO launch path is not an owned file or symlink')
    if str(path.resolve(strict=True)) != target:
        raise RoomError('AO launch path does not resolve to the replacement executable')
    return {'path': str(path), 'resolved_path': target,
            'symlink': os.readlink(path) if path.is_symlink() else None}


def _read(directory, pointer):
    import ao_project_room as ao
    if not isinstance(pointer, dict) or set(pointer) != {'path', 'sha256'}:
        raise RoomError('Malformed executable binding pointer')
    relative = pointer['path']
    if not isinstance(relative, str) or not relative.startswith(BASE + '/') or not relative.endswith('.json'):
        raise RoomError('Malformed executable binding path')
    ao.identifier(relative[len(BASE) + 1:-5])
    result = json.loads(owned_bytes(directory / relative))
    if ao.digest(result) != pointer['sha256']:
        raise RoomError('Immutable executable binding evidence changed')
    return result


def _chain(directory, state, prepared):
    import ao_project_room as ao
    pointer = state.get('executable_binding')
    found, latest, records = set(), None, []
    while pointer is not None:
        if not isinstance(pointer, dict):
            raise RoomError('Malformed executable binding pointer')
        if len(found) >= 32 or pointer.get('path') in found:
            raise RoomError('Executable binding history is cyclic or exceeds its bound')
        record = _read(directory, pointer)
        if (record.get('version') != 1 or record.get('room_id') != state['room_id']
                or record.get('engineer') != state['bindings'].get('engineer')
                or record.get('preparation') != state['preparation']
                or record.get('preparation_sha256') != state['preparation_sha256']
                or ao.digest(prepared) != state['preparation_sha256']
                or pointer['path'] != BASE + '/' + record['inputs']['request_id'] + '.json'):
            raise RoomError('Executable binding does not match the unchanged engineer/preparation')
        if latest is None:
            latest = record
        found.add(pointer['path']); records.append(record)
        pointer = record['previous']
    files = {str(p.relative_to(directory)) for p in (directory / BASE).glob('*.json')}
    if files != found:
        raise RoomError('Unclaimed or missing executable binding intent; reconcile the exact request before continuing')
    prior = (prepared.get('routing') or {}).get('claude')
    for record in reversed(records):
        if record.get('source') != prior:
            raise RoomError('Executable binding source history changed')
        prior = record['target']
    return latest


def effective(directory, state, prepared):
    """Validate both the original record and the separate current executable."""
    record = _chain(directory, state, prepared)
    if record is None:
        return None
    target = record['target']
    if _fingerprint(target['path']) != {k: target[k] for k in ('path', 'size', 'mtime_ns', 'sha256')}:
        raise RoomError('Rebound Claude executable changed')
    if _launch(record['launch']['path'], target['path']) != record['launch']:
        raise RoomError('Rebound AO launch path changed')
    owner = read_owner(record['inputs']['database_path'], record['engineer']['session_id'])
    original = record['evidence']['native_owner']
    stable = set(original) - {'activity_state', 'controller_generation', 'strategy'}
    if set(original) != set(owner) or any(original[k] != owner[k] for k in stable):
        raise RoomError('Rebound executable native owner changed')
    return target


def _source_failure(source):
    if not isinstance(source, dict) or not source.get('path') or not source.get('version') or source.get('error'):
        raise RoomError('Executable repair requires a previously recorded successful identity')
    try:
        current = _fingerprint(source['path']) if source.get('sha256') else None
        info = Path(source['path']).stat()
    except FileNotFoundError:
        return 'missing'
    if ((info.st_size, info.st_mtime_ns) != (source['size'], source['mtime_ns'])
            or current is not None and current['sha256'] != source['sha256']):
        return 'changed'
    raise RoomError('Recorded executable is unchanged; this operation only repairs diagnosed loss or drift')


def _inspect(service, directory, state, inputs, prepared, source):
    import ao_project_room as ao
    import ao_routing
    import ao_workflow
    from ao_provider_transition import _CompleteClient
    from ao_routing_adoption import _ReadOnly
    if (not ao_workflow.normal(state)
            or state['bindings'].get('engineer', {}).get('reasoning_effort') != 'max'
            or state['bindings'].get('engineer', {}).get('model') != ao_workflow.FABLE_MODEL):
        raise RoomError('Executable repair requires the existing normal Fable MAX engineer')
    if any(r['state'] not in ('completed', 'settled_failure') for r in state['requests'].values()):
        raise RoomError('An unsettled owned request prevents executable repair')
    failure = _source_failure(source)
    target = _fingerprint(inputs['executable_path'])
    version = ao_routing.claude_evidence(target['path'])
    if version.get('error') or not version.get('version') or any(version[k] != target[k] for k in ('path', 'size', 'mtime_ns')):
        raise RoomError('Replacement Claude version probe failed or identity changed')
    if _fingerprint(target['path']) != target:
        raise RoomError('Replacement executable changed during its version probe')
    target.update(version=version['version'], error=None)
    launch = _launch(inputs['launch_path'], target['path'])
    from ao_routing_refresh import effective as refreshed_routing
    # Validate routing ancestry against the original immutable preparation before
    # using its current file pins in this local replacement-executable probe.
    # Neither journal may receive the synthetic preparation below.
    routing = refreshed_routing(directory, state, prepared)
    current = copy.deepcopy(prepared)
    if routing is not None:
        current['routing'] = routing
    current['routing']['claude'] = target
    ao_routing.validate_local(current)  # All existing guard/configuration checks remain required.
    engineer = state['bindings']['engineer']
    snapshot = ao.Service.identity(_ReadOnly(service), _CompleteClient(service.client(state)), state, engineer)
    if snapshot.get('controller') != 'stopped':
        raise RoomError('Stop the idle native engineer through AO before executable repair')
    if any(t.get('state') not in ('completed', 'failed', 'cancelled', 'recovered') for t in snapshot.get('turns', [])):
        raise RoomError('Unsettled native history prevents executable repair')
    owner = read_owner(inputs['database_path'], engineer['session_id'])
    if (owner['ao_conversation_id'] != engineer['conversation_id'] or owner['active_branch_id'] != engineer['branch_id']
            or owner['workspace_path'] != prepared['worktree'] or owner['project_id'] != state['ao_project_id']):
        raise RoomError('Native owner does not match the recorded engineer workspace and branch')
    return {'source_failure': failure, 'native_owner': owner,
            'public_history_sha256': ao.digest({k: snapshot.get(k) for k in ('turns', 'messages', 'activities')}),
            'settings': snapshot.get('settings'), 'controller': 'stopped',
            'requests_sha256': ao.digest(state['requests']), 'target': target, 'launch': launch}


def bind(service, room_id, request_id, executable_path, launch_path, database_path, authorization, diagnosis):
    """Append one exact authorized repair; no prompts, lifecycle or old-pin edits."""
    import ao_project_room as ao
    import ao_delegates
    ao.identifier(request_id)
    ao.nonempty(authorization, 'authorization', 6000); ao.nonempty(diagnosis, 'diagnosis', 6000)
    inputs = dict(request_id=request_id, executable_path=executable_path, launch_path=launch_path,
                  database_path=database_path, authorization=authorization, diagnosis=diagnosis)
    with service.locked(room_id) as (directory, state):
        prepared = ao_delegates.validate_preparation(directory, state, state['bindings']['engineer']['session_id'], check_routing=False)
        relative = BASE + '/' + request_id + '.json'
        target_path = directory / relative
        existing = json.loads(owned_bytes(target_path)) if target_path.exists() else None
        if existing is not None and existing['inputs'] != inputs:
            raise RoomError('Executable binding request already has different immutable inputs')
        pointer = {'path': relative, 'sha256': ao.digest(existing)} if existing is not None else None
        if existing is not None and state.get('executable_binding') == pointer:
            effective(directory, state, prepared)
            return {**pointer, 'model_dispatch': False, 'idempotent': True}
        from ao_review_extension import guard_pending_receipts
        guard_pending_receipts(directory, state)
        # A crash between durable intent and state commit may only replay this
        # exact intent against the identical state and fresh local/native evidence.
        if existing is not None:
            if existing['before_state_sha256'] != ao.digest(state):
                raise RoomError('Pending executable repair state changed; preserve evidence and diagnose')
            source = existing['source']
        else:
            prior = _chain(directory, state, prepared)
            source = prior['target'] if prior else prepared['routing']['claude']
        evidence = _inspect(service, directory, state, inputs, prepared, source)
        from ao_review_extension import guard_native_owner
        guard_native_owner(service, state, evidence['native_owner'])
        record = existing or {'version': 1, 'room_id': room_id, 'inputs': inputs,
            'recorded_at': time.time(), 'before_state_sha256': ao.digest(state),
            'engineer': state['bindings']['engineer'], 'preparation': state['preparation'],
            'preparation_sha256': state['preparation_sha256'], 'previous': state.get('executable_binding'),
            'source': source, 'target': evidence['target'], 'launch': evidence['launch'], 'evidence': evidence}
        if record['evidence'] != evidence:
            raise RoomError('Pending executable repair evidence changed; diagnose without rewriting its intent')
        if existing is None:
            _store_once(target_path, record)
        pointer = {'path': relative, 'sha256': ao.digest(record)}
        proposed = {**state, 'executable_binding': pointer}
        effective(directory, proposed, prepared)
        service.save(directory, proposed)
        return {**pointer, 'model_dispatch': False, 'idempotent': False,
                'original_preparation_preserved': True, 'controller': 'stopped', 'claude': record['target']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('home', 'room-id', 'request-id', 'executable-path', 'launch-path', 'database-path', 'authorization', 'diagnosis'):
        parser.add_argument('--' + name, required=True)
    args = vars(parser.parse_args())
    from ao_project_room import Service
    service = Service(args.pop('home'))
    print(json.dumps(bind(service, **args), indent=2))


if __name__ == '__main__':
    main()
