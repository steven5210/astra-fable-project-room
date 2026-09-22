"""Audited, CLI-only refresh of a stopped existing v2 native routing bundle.

Original preparation, provider, review and outcome records remain immutable. One
append-only intent binds exact source/target bytes before three ignored runtime
files are replaced. No native lifecycle, prompt, model, or provider operation is
performed. A crash permits only the identical intent against its unchanged state
and evidence; ordinary routing validation rejects every unclaimed intent.
"""

import argparse
import copy
import json
import os
from pathlib import Path
import tempfile
import time
import uuid

import ao_delegates
import ao_project_room as ao
import ao_routing
import ao_workflow
from ao_delegate_launcher import owned_bytes
from ao_native_identity import read_owner
from implementation import candidate_snapshot
from room import RoomError

BASE = 'routing-refresh'
INTENT_STAGING = BASE + '-staging'
MAX_CHAIN = 32
MAX_RECORD_BYTES = 96_000_000
CHANGED_ROUTING = {'files', 'guard_path', 'guard_sha256', 'hook_command'}
ACTIVITY_FIELDS = ('id', 'sequence', 'turnId', 'type', 'status', 'summary', 'createdAt', 'updatedAt', 'startedAt', 'finishedAt')


def _json(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False) + '\n').encode()


def _write_intent(fd, raw):
    """Finish a bounded staging write; a short write is never a complete intent."""
    remaining = memoryview(raw)
    while remaining:
        written = os.write(fd, remaining[:1024 * 1024])
        if written <= 0:
            raise OSError('Routing refresh intent write made no progress')
        remaining = remaining[written:]


def _publish_intent(path, record):
    """Publish complete, durable JSON without overwriting any existing journal.

    A killed writer may leave a partial file in the separate staging directory.
    It has no journal authority and cannot block an identical retry. Published
    records always retain the ordinary immutable-intent reconciliation rules.
    """
    raw = _json(record)
    if len(raw) > MAX_RECORD_BYTES:
        raise RoomError('Routing refresh intent exceeds its bounded readable size')
    staging = path.parent.parent / INTENT_STAGING
    directories = []
    temporary = None
    fd = None
    try:
        for folder in (path.parent, staging):
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            if any(parent.is_symlink() for parent in (folder, *folder.parents)):
                raise RoomError('Routing refresh intent directory traverses a symlink')
            directory = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            directories.append(directory)
            metadata = os.fstat(directory)
            if metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
                raise RoomError('Routing refresh intent directory ownership is unsafe')
        journal_fd, staging_fd = directories
        # Persist newly created journal/staging directory entries before any
        # published intent may authorize runtime changes.
        parent_fd = os.open(path.parent.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        temporary = path.stem + '.' + uuid.uuid4().hex + '.tmp'
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=staging_fd)
        _write_intent(fd, raw)
        os.fsync(fd)
        os.close(fd)
        fd = None
        # link is an atomic no-clobber publication on the same filesystem.
        # An existing final file, even corrupt or partial, is never overwritten.
        os.link(temporary, path.name, src_dir_fd=staging_fd, dst_dir_fd=journal_fd, follow_symlinks=False)
        os.fsync(journal_fd)
    finally:
        if fd is not None:
            os.close(fd)
        if temporary is not None:
            try:
                os.unlink(temporary, dir_fd=directories[1])
            except FileNotFoundError:
                pass
        for directory in reversed(directories):
            os.close(directory)


def _read(directory, pointer):
    if not isinstance(pointer, dict) or set(pointer) != {'path', 'sha256'}:
        raise RoomError('Malformed routing refresh pointer')
    relative = pointer['path']
    if not isinstance(relative, str) or not relative.startswith(BASE + '/') or not relative.endswith('.json'):
        raise RoomError('Malformed routing refresh journal path')
    ao.identifier(relative[len(BASE) + 1:-5])
    record = json.loads(owned_bytes(directory / relative, MAX_RECORD_BYTES))
    if not isinstance(record, dict) or ao.digest(record) != pointer['sha256']:
        raise RoomError('Immutable routing refresh record changed')
    return record


def _entries(directory):
    root = directory / BASE
    if not root.exists() and not root.is_symlink():
        return set()
    if root.is_symlink() or not root.is_dir():
        raise RoomError('Routing refresh journal directory is unsafe')
    entries = set()
    for path in root.iterdir():
        if len(entries) >= MAX_CHAIN or path.suffix != '.json' or path.is_symlink() or not path.is_file():
            raise RoomError('Routing refresh journal contains unsafe, extra or excessive entries')
        ao.identifier(path.stem)
        entries.add(str(path.relative_to(directory)))
    return entries


def _bundle_valid(routing, bundle):
    if (not isinstance(routing, dict) or type(routing.get('version')) is not int or routing['version'] != 2
            or routing.get('execution_policy') != ao_routing.EXECUTION_POLICY
            or routing.get('matcher') != ao_routing.MATCHER or routing.get('agents') != ao_routing.MODELS
            or routing.get('effort') != 'max' or set(routing.get('files', {})) != set(ao_routing.FILES)
            or not isinstance(bundle, dict) or set(bundle) != {'files', 'guard'}
            or set(bundle['files']) != set(ao_routing.FILES)):
        raise RoomError('Routing refresh requires a complete pinned v2 MAX bundle')
    for relative, text in bundle['files'].items():
        if not isinstance(text, str) or ao.digest(text.encode()) != routing['files'][relative]:
            raise RoomError('Archived routing refresh file bytes changed')
        if relative.endswith('.md'):
            ao_routing.validate_definition(Path(relative).stem, text)
        else:
            ao_routing.check_settings(json.loads(text), routing)
    if (not isinstance(bundle['guard'], str) or ao.digest(bundle['guard'].encode()) != routing['guard_sha256']
            or routing['hook_command'] != ao_routing.hook_command(routing['python'], routing['guard_path'])):
        raise RoomError('Archived routing refresh guard bytes or command changed')


def _chain(directory, state, prepared, *, check_unclaimed=True):
    """Validate committed immutable ancestry; return its latest record or None.

    Only the review-extension ancestry check may omit extra-intent enumeration so
    the exact refresh can reconcile. Public effective validation never omits it.
    This function does not inspect current mutable runtime files.
    """
    pointer = state.get('routing_refresh')
    found, records = set(), []
    while pointer is not None:
        if not isinstance(pointer, dict) or pointer.get('path') in found or len(found) >= MAX_CHAIN:
            raise RoomError('Routing refresh history is malformed, cyclic or exceeds its bound')
        record = _read(directory, pointer)
        if (record.get('version') != 1 or record.get('room_id') != state['room_id']
                or record.get('engineer') != state['bindings'].get('engineer')
                or record.get('preparation') != state['preparation']
                or record.get('preparation_sha256') != state['preparation_sha256']
                or ao.digest(prepared) != state['preparation_sha256']
                or record.get('delegate_sha256') != ao.digest(state['delegate'])
                or pointer['path'] != BASE + '/' + record['inputs']['request_id'] + '.json'):
            raise RoomError('Routing refresh differs from the unchanged engineer, provider or preparation')
        _bundle_valid(record['source'], record['source_bundle'])
        _bundle_valid(record['target'], record['target_bundle'])
        if ({k: v for k, v in record['source'].items() if k not in CHANGED_ROUTING}
                != {k: v for k, v in record['target'].items() if k not in CHANGED_ROUTING}):
            raise RoomError('Routing refresh changed pinned model, effort or unrelated routing policy')
        if record['target']['guard_path'] != str(directory.parent.parent / 'launchers' / (record['target']['guard_sha256'] + '.py')):
            raise RoomError('Routing refresh target guard is outside its content-addressed launcher directory')
        for routing in (record['source'], record['target']):
            if ao.digest(owned_bytes(routing['guard_path'])) != routing['guard_sha256']:
                raise RoomError('Retained routing refresh guard was modified')
        found.add(pointer['path'])
        records.append(record)
        pointer = record['previous']
    if check_unclaimed and _entries(directory) != found:
        raise RoomError('Unclaimed or missing routing refresh intent; reconcile the exact request before continuing')
    prior = prepared.get('routing')
    prior_bundle = None
    for record in reversed(records):
        if record['source'] != prior or prior_bundle is not None and record['source_bundle'] != prior_bundle:
            raise RoomError('Routing refresh source-to-target continuity changed')
        prior, prior_bundle = record['target'], record['target_bundle']
    return records[0] if records else None


def _same_owner(record, owner):
    original = record['evidence']['native_owner']
    stable = set(original) - {'activity_state', 'controller_generation', 'strategy'}
    if set(owner) != set(original) or any(owner[key] != original[key] for key in stable):
        raise RoomError('Routing refresh native owner changed')


def effective(directory, state, prepared):
    record = _chain(directory, state, prepared)
    if record is None:
        return None
    for relative, expected in record['target']['files'].items():
        ao_routing._target(Path(prepared['worktree']), relative)
        ao_routing._require_ignored(Path(prepared['worktree']), relative)
        if ao.digest(owned_bytes(Path(prepared['worktree']) / relative)) != expected:
            raise RoomError('Current routing refresh file differs from its committed target: ' + relative)
    owner = read_owner(record['inputs']['database_path'], record['engineer']['session_id'])
    _same_owner(record, owner)
    return copy.deepcopy(record['target'])


def _build(service, prepared, source):
    source_bundle = {'files': {relative: owned_bytes(Path(prepared['worktree']) / relative).decode()
                               for relative in ao_routing.FILES},
                     'guard': owned_bytes(source['guard_path']).decode()}
    _bundle_valid(source, source_bundle)
    guard = owned_bytes(Path(__file__).with_name('ao_routing_guard.py')).decode()
    target = copy.deepcopy(source)
    target['guard_sha256'] = ao.digest(guard.encode())
    target['guard_path'] = str(service.root / 'launchers' / (target['guard_sha256'] + '.py'))
    target['hook_command'] = ao_routing.hook_command(target['python'], target['guard_path'])
    original_settings = json.loads(source_bundle['files']['.claude/settings.local.json'])
    settings = ao_routing.foreground_settings(copy.deepcopy(original_settings))
    entry = {'matcher': source['matcher'], 'hooks': [{'type': 'command', 'command': source['hook_command'], 'timeout': 30}]}
    hooks = settings['hooks']['PreToolUse']
    if hooks.count(entry) != 1:
        raise RoomError('The exact source routing hook is missing or duplicated')
    settings['hooks']['PreToolUse'] = [
        {'matcher': target['matcher'], 'hooks': [{'type': 'command', 'command': target['hook_command'], 'timeout': 30}]}
        if item == entry else item for item in hooks]
    files = {'.claude/settings.local.json': (_json(settings).decode() if settings != original_settings
             else source_bundle['files']['.claude/settings.local.json'])}
    files.update({'.claude/agents/' + name + '.md': ao_routing.agent_definition(name) for name in ao_routing.MODELS})
    target['files'] = {relative: ao.digest(text.encode()) for relative, text in files.items()}
    target_bundle = {'files': files, 'guard': guard}
    _bundle_valid(target, target_bundle)
    if source == target and source_bundle == target_bundle:
        raise RoomError('Routing already matches the installed bundle; no refresh is needed')
    return target, source_bundle, target_bundle


def _retained_manifest(directory, state):
    from ao_review_extension import _manifest, BASE as extension_base
    from ao_provider_transition import BASE as provider_base
    from ao_routing_adoption import BASE as adoption_base
    result = _manifest(directory, state)
    # Initial ordinary validation verifies these journals. Exact reconciliation
    # additionally requires the same complete immutable records, without calling
    # ordinary routing validation on an intentionally mixed source/target tree.
    for folder in (provider_base, adoption_base, 'reviewer-recovery', extension_base):
        for path in (directory / folder).rglob('*'):
            if path.is_symlink():
                raise RoomError('Retained routing refresh evidence traverses a symlink')
            if path.is_file():
                if len(result) >= 4096:
                    raise RoomError('Retained routing refresh evidence exceeds its file bound')
                result[str(path.relative_to(directory))] = ao.digest(owned_bytes(path, MAX_RECORD_BYTES))
    return result


def _inspect(service, directory, state, inputs, prepared, routing):
    from ao_provider_transition import _CompleteClient, _ReadOnlyIdentity, _native, _ledger_rows, _ledger_check
    from ao_review_extension import guard_pending_receipts, guard_routing_refresh
    from ao_outcomes import validate_settlement
    guard_pending_receipts(directory, state)
    engineer = state['bindings'].get('engineer') or {}
    if (not ao_workflow.normal(state) or engineer.get('harness') != 'claude-code'
            or engineer.get('model') != ao_workflow.FABLE_MODEL or engineer.get('reasoning_effort') != 'max'):
        raise RoomError('Routing refresh requires the existing native Fable MAX engineer')
    for request in state['requests'].values():
        if (request.get('state') not in ('completed', 'settled_failure') or request.get('model_reroute')
                or not ao.native_turn_identity(request)):
            raise RoomError('Unsettled owned request prevents routing refresh')
        if request['state'] == 'settled_failure':
            validate_settlement(directory, request)
    if any(item.get('state') == 'running' for item in state['verifications']):
        raise RoomError('An unfinished verification prevents routing refresh')
    ao_delegates.validate_preparation(directory, state, engineer['session_id'], check_routing=False)
    ao_delegates.assert_settled(service.root.parent, copy.deepcopy(state), directory)
    ledger_present, rows = _ledger_rows(service.root.parent, state['room_id'])
    _ledger_check(rows)
    snapshot = _ReadOnlyIdentity(service).identity(_CompleteClient(service.client(state)), state, engineer)
    if snapshot.get('controller') != 'stopped':
        raise RoomError('Stop the idle native engineer through AO before routing refresh')
    native = _native(state, engineer, snapshot, directory)
    owner = read_owner(inputs['database_path'], engineer['session_id'])
    if (owner['provider_conversation_id'] != inputs['native_session_id'] or owner['project_id'] != state['ao_project_id']
            or owner['workspace_path'] != prepared['worktree'] or owner['ao_conversation_id'] != engineer['conversation_id']
            or owner['active_branch_id'] != engineer['branch_id'] or owner['activity_state'] not in ('idle', 'exited')):
        raise RoomError('Routing refresh requires the same stopped native owner, workspace and branch')
    extension = guard_routing_refresh(service, state, owner)
    import ao_acceptance_extension
    ao_acceptance_extension.guard_unused(service, state, 'routing refresh')
    from ao_executable_binding import effective as executable
    replacement = executable(directory, state, prepared)  # Always the unchanged original preparation.
    ao_routing.check_claude(replacement or routing.get('claude') or {})
    ao_routing.contradictions(routing['claude_config_dir'], Path(prepared['worktree']))
    observed = ao_routing.observe_rules(_CompleteClient(service.client(state)), state)
    if not ao_routing.rules_match(observed, routing['rules']):
        raise RoomError('AO project routing rules changed before refresh')
    if 'compaction' in routing:
        ao_routing._compaction_sources(routing['compaction'], routing['claude_config_dir'], Path(prepared['worktree']), os.environ)
        ao_routing._compaction_project(_CompleteClient(service.client(state)), state, routing['compaction'])
    # Keep the full original snapshot in the immutable intent, but compare the
    # observed native/history/settings and activity facts rather than changing
    # elapsed-time or observation-age fields on the public response envelope.
    activities = sorted(({key: item[key] for key in ACTIVITY_FIELDS if key in item}
                         for item in snapshot['activities']), key=lambda item: item['id'])
    return {'native_owner': owner, 'native': native, 'settings': snapshot.get('settings'),
            'activities': activities, 'review_extension': extension,
            'ledger_present': ledger_present, 'ledger_rows': rows, 'requests_sha256': ao.digest(state['requests']),
            'candidate': candidate_snapshot(Path(prepared['worktree'])), 'retained_manifest': _retained_manifest(directory, state),
            'rules': observed, 'executable_binding': state.get('executable_binding'), 'current_executable': replacement}, snapshot


def _sync_directory(path):
    directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _place_guard(path, raw):
    """Publish a complete content-addressed file; a partial temp is never its pin."""
    if path.exists() or path.is_symlink():
        if owned_bytes(path) != raw:
            raise RoomError('Current content-addressed routing guard is corrupted')
        _sync_directory(path.parent.parent)  # A preceding mkdir may still owe its parent-directory barrier.
        _sync_directory(path.parent)  # An earlier link may have succeeded before its directory sync failed.
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if any(parent.is_symlink() for parent in (path.parent, *path.parent.parents)):
        raise RoomError('Routing guard directory traverses a symlink')
    _sync_directory(path.parent.parent)  # Persist the launcher directory entry before publishing a guard within it.
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.pending-routing-guard-')
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            if owned_bytes(path) != raw:
                raise RoomError('Routing guard appeared with different bytes')
        _sync_directory(path.parent)
    finally:
        os.unlink(temporary)


def _replace_runtime(worktree, relative, source, target):
    # Recover only an exact prefix left by the existing atomic writer after the
    # durable intent. Unknown bytes remain evidence and block reconciliation.
    path = worktree / relative
    pending = path.with_name('.pending-' + path.name)
    if pending.exists() or pending.is_symlink():
        data = owned_bytes(pending)
        if not target.startswith(data) or pending.stat().st_mode & 0o022:
            raise RoomError('Pending routing refresh bytes differ from the exact target')
        pending.unlink()
    ao_routing._target(worktree, relative)
    ao_routing._require_ignored(worktree, relative)
    actual = owned_bytes(path)
    if actual != target:
        if actual != source:
            raise RoomError('Routing refresh file matches neither recorded source nor target: ' + relative)
        ao_routing._write(worktree, relative, target, previous=source)
    # Exact target bytes do not prove that a previous rename is durable.
    _sync_directory(path.parent)


def refresh(service, room_id, request_id, database_path, native_session_id, authorization, diagnosis):
    ao.identifier(request_id)
    for name, value in (('database_path', database_path), ('native_session_id', native_session_id),
                        ('authorization', authorization), ('diagnosis', diagnosis)):
        ao.nonempty(value, name, 6000)
    inputs = dict(request_id=request_id, database_path=database_path, native_session_id=native_session_id,
                  authorization=authorization, diagnosis=diagnosis)
    with service.locked(room_id) as (directory, state):
        prepared = ao_delegates.validate_preparation(directory, state, check_routing=False)
        relative = BASE + '/' + request_id + '.json'
        path = directory / relative
        existing = json.loads(owned_bytes(path, MAX_RECORD_BYTES)) if path.exists() or path.is_symlink() else None
        if existing is not None and existing.get('inputs') != inputs:
            raise RoomError('Routing refresh request already has different immutable inputs')
        pointer = {'path': relative, 'sha256': ao.digest(existing)} if existing is not None else None
        if existing is not None and state.get('routing_refresh') == pointer:
            effective(directory, state, prepared)
            _sync_directory(directory)  # Retry a state replacement whose final directory sync failed.
            return {**pointer, 'model_dispatch': False, 'idempotent': True}
        prior = _chain(directory, state, prepared, check_unclaimed=existing is None)
        source = prior['target'] if prior else prepared.get('routing')
        if existing is None:
            # Reserve capacity before publishing an intent or changing runtime
            # files; otherwise the new record would exceed its own read bound.
            if len(_entries(directory)) >= MAX_CHAIN:
                raise RoomError('Routing refresh history is at capacity; no new intent was written')
            service.settled(state)
            ao_routing.validate_local(prepared, state, directory)
            target, source_bundle, target_bundle = _build(service, prepared, source)
        else:
            committed = set()
            previous = state.get('routing_refresh')
            while previous is not None:
                committed.add(previous['path']); previous = _read(directory, previous)['previous']
            if (_entries(directory) != committed | {relative} or existing['before_state_sha256'] != ao.digest(state)
                    or existing['previous'] != state.get('routing_refresh') or existing['source'] != source
                    or existing['preparation_sha256'] != state['preparation_sha256']
                    or existing['engineer'] != state['bindings'].get('engineer')):
                raise RoomError('Pending routing refresh state or ancestry changed; preserve and diagnose')
            target, source_bundle, target_bundle = existing['target'], existing['source_bundle'], existing['target_bundle']
            _bundle_valid(source, source_bundle); _bundle_valid(target, target_bundle)
            if (existing.get('version') != 1 or existing.get('room_id') != room_id
                    or existing.get('preparation') != state['preparation']
                    or existing.get('delegate_sha256') != ao.digest(state['delegate'])
                    or {k: v for k, v in source.items() if k not in CHANGED_ROUTING}
                    != {k: v for k, v in target.items() if k not in CHANGED_ROUTING}
                    or target['guard_path'] != str(service.root / 'launchers' / (target['guard_sha256'] + '.py'))):
                raise RoomError('Pending routing refresh changed pinned policy or target identity')
            if ao.digest(owned_bytes(source['guard_path'])) != source['guard_sha256']:
                raise RoomError('Original routing guard changed during refresh')
        # Infer the new mode only from the immutable target settings; old pending
        # records remain reproducible and an exact retry never acquires defaults.
        target_env = json.loads(target_bundle['files']['.claude/settings.local.json']).get('env', {})
        foreground = all(target_env.get(key) == value for key, value in ao_routing.FOREGROUND_ENV.items())

        def check_foreground():
            if foreground:
                ao_routing.contradictions(source['claude_config_dir'], Path(prepared['worktree']), os.environ, foreground=True)
                ao_routing._foreground_project(service.client(state), state)

        check_foreground()
        evidence, observed_snapshot = _inspect(service, directory, state, inputs, prepared, source)
        record = existing or {'version': 1, 'room_id': room_id, 'inputs': inputs, 'recorded_at': time.time(),
            'before_state_sha256': ao.digest(state), 'engineer': state['bindings']['engineer'],
            'preparation': state['preparation'], 'preparation_sha256': state['preparation_sha256'],
            'delegate_sha256': ao.digest(state['delegate']), 'previous': state.get('routing_refresh'),
            'source': source, 'target': target, 'source_bundle': source_bundle, 'target_bundle': target_bundle,
            'evidence': evidence, 'observed_snapshot': observed_snapshot}
        if record['evidence'] != evidence:
            raise RoomError('Pending routing refresh evidence changed; preserve its immutable intent')
        if len(_json(record)) > MAX_RECORD_BYTES:
            raise RoomError('Routing refresh intent exceeds its bounded readable size')
        proposed = {**state, 'routing_refresh': {'path': relative, 'sha256': ao.digest(record)}}
        from ao_routing_adoption import launcher_compatibility
        launcher_compatibility(service, proposed, prepared)
        if existing is None:
            _publish_intent(path, record)
        else:
            # A prior process may have stopped after linking the complete file
            # but before persisting its directory entry. Reestablish that
            # durability barrier before the exact retry changes runtime files.
            journal_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                os.fsync(journal_fd)
            finally:
                os.close(journal_fd)
        _place_guard(Path(target['guard_path']), target_bundle['guard'].encode())
        for name in ao_routing.FILES:
            _replace_runtime(Path(prepared['worktree']), name, source_bundle['files'][name].encode(), target_bundle['files'][name].encode())
        check_foreground()
        if _inspect(service, directory, state, inputs, prepared, source)[0] != record['evidence']:
            raise RoomError('Routing refresh evidence changed before commit; leave the exact intent pending')
        effective(directory, proposed, prepared)
        service.save(directory, proposed)
        return {**proposed['routing_refresh'], 'model_dispatch': False, 'idempotent': False,
                'original_preparation_preserved': True, 'controller': 'stopped',
                'guard_sha256': target['guard_sha256'], 'files': target['files']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('home', 'room-id', 'request-id', 'database-path', 'native-session-id', 'authorization', 'diagnosis'):
        parser.add_argument('--' + name, required=True)
    args = vars(parser.parse_args())
    service = ao.Service(args.pop('home'))
    print(json.dumps(refresh(service, **args), indent=2))


if __name__ == '__main__':
    main()
