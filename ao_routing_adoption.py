"""One audited v1 routing adoption after a provider amendment, before new work.

The operator performs AO lifecycle and the exact staged project-config update.
This module uses GETs only and never launches a model or changes provider jobs.
"""
import copy
import json
import os
from pathlib import Path
import re
import time
import uuid

import ao_project_room as ao
import ao_delegates
import ao_routing
import ao_workflow
from ao_delegate_launcher import owned_bytes
from ao_reviewer_recovery import _store_once
from implementation import candidate_snapshot
from room import RoomError

BASE = 'routing-adoption'
REL = BASE + '/v2'
INSTRUCTION = (
    'User-authorized operating amendment: subsequent delegate work uses the official DeepSeek API '
    'model deepseek-flash at reasoning effort max, max_tokens 393216 and context_tokens 1048576. '
    'This supersedes earlier operating instructions selecting DeepInfra or forbidding official DeepSeek; '
    'the historical specification, source requirements, agreement and consumed review attempts remain unchanged. '
    'Fable remains the MAX engineering orchestrator and final engineering reviewer. '
    'Use DeepSeek for suitable substantive work, and the pinned Sonnet or Opus workers when needed for full quality. '
    'The v2 guard requires execution to remain delegated: root shell/edit/test/browser execution is denied. '
    'The assigned operator or identified native worker executes checks requested by Fable. '
    'No inherited Fable workers, extra native delegation layer or more than two concurrent native workers. '
    'Do not replay earlier provider jobs or resend the unchanged specification or policies on continuation.'
)
TOOLS = ['deepseek_submit', 'deepseek_ask', 'deepseek_status', 'deepseek_result', 'deepseek_cancel', 'deepseek_health']
MAX_EVIDENCE = 96_000_000
LEGACY_LAUNCH_LIMIT = 4_000_000


def _json(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()


def _path(directory, relative):
    p = Path(relative)
    if p.is_absolute() or '..' in p.parts or not str(p).startswith(BASE + '/'):
        raise RoomError('Invalid routing-adoption evidence path')
    return directory / p


def _load(directory, relative, expected):
    value = json.loads(owned_bytes(_path(directory, relative), MAX_EVIDENCE))
    if ao.digest(value) != expected:
        raise RoomError('Routing-adoption evidence changed')
    return value


def launcher_compatibility(service, state, prepared, reserve_bytes=0):
    """Preflight the unchanged launcher's read ceiling, including proposed pointers.

    This is a current size check, not a guarantee about future history growth.
    The frozen launcher reads every state and every normal active preparation.
    """
    try:
        largest, count = 0, 0
        for path in (service.root / 'rooms').glob('*/state.json'):
            raw = owned_bytes(path, LEGACY_LAUNCH_LIMIT)
            other = json.loads(raw)
            largest, count = max(largest, len(raw)), count + 1
            if other.get('workflow') == 'fable_engineering' and other.get('preparation'):
                data = owned_bytes(path.parent / other['preparation'], LEGACY_LAUNCH_LIMIT)
                json.loads(data)
                largest = max(largest, len(data))
        # State uses ao.atomic's exact encoding; preparations use the immutable
        # indented writer. Check future pointers before their intent is saved.
        proposed_state = (json.dumps(state, ensure_ascii=False, sort_keys=True, allow_nan=False) + '\n').encode()
        sizes = (len(proposed_state), len(_json(prepared)))
        if sizes[0] + reserve_bytes > LEGACY_LAUNCH_LIMIT or sizes[1] > LEGACY_LAUNCH_LIMIT:
            raise ValueError('proposed evidence exceeds the retained launcher ceiling')
        return {'limit_bytes': LEGACY_LAUNCH_LIMIT, 'states_checked': count,
                'largest_current_bytes': largest, 'proposed_state_bytes': sizes[0],
                'proposed_preparation_bytes': sizes[1]}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RoomError('Retained launcher compatibility requires every scanned state and preparation to be readable within 4,000,000 bytes') from exc


def probe_evidence(home, room_id):
    """Observe paid CLI probes too; they are not represented in the job ledger."""
    import deepseek_adapter as adapter
    from ao_provider_transition import ELIGIBLE_STATES, SOURCE
    path = Path(home) / 'deepseek' / 'probes'
    if not path.exists() and not path.is_symlink():
        return []
    try:
        fd = adapter.open_directory(path, 'probes_unsafe')
        try:
            with os.scandir(fd) as entries:
                names, directories = set(), set()
                for entry in entries:
                    if len(names) >= adapter.MAX_PROBE_ENTRIES:
                        raise RoomError('Probe history exceeds the complete adoption scan bound')
                    names.add(entry.name)
                    if entry.is_dir(follow_symlinks=False):
                        directories.add(entry.name)
            receipts = {n for n in names if adapter.PROBE_NAME.fullmatch(n)}
            if names != receipts | {n[:-5] for n in receipts} or directories != {n[:-5] for n in receipts}:
                raise RoomError('Unowned or incomplete probe evidence refuses adoption')
            result = []
            for name in sorted(receipts):
                value = adapter.read_json_below(fd, (name,), 65536, 'probes_unsafe')
                if (not isinstance(value, dict) or value.get('kind') != 'deepseek_probe'
                        or not isinstance(value.get('room_id'), str) or not value['room_id']
                        or value.get('artifacts') != name[:-5]):
                    raise RoomError('Probe ownership evidence is unavailable')
                if value['room_id'] != room_id:
                    continue
                requested = value.get('requested') or {}
                if (not isinstance(requested, dict) or value.get('backend') != SOURCE['backend']
                        or any(requested.get(k) != v for k, v in SOURCE.items())):
                    raise RoomError('Routing adoption must precede every target-provider probe')
                if value.get('state') not in ELIGIBLE_STATES:
                    raise RoomError('Unsettled source-provider probe refuses adoption')
                result.append({'receipt': name, 'sha256': ao.digest(value)})
            return result
        finally:
            os.close(fd)
    except (OSError, ValueError, KeyError, TypeError, adapter.AdapterError) as exc:
        raise RoomError('Complete paid-probe evidence is unreadable or inconsistent') from exc


def _audit(directory, digest):
    if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
        raise RoomError('Use the exact saved routing-adoption audit digest')
    return _load(directory, BASE + '/audits/' + digest + '.json', digest)


def pending_gate(service, state):
    directory = service.root / 'rooms' / state['room_id']
    ref = state.get('routing_adoption')
    pending = list((directory / BASE / 'requests').glob('*.json'))
    if ref is None:
        if pending:
            raise RoomError('Unclaimed routing-adoption intent exists; reconcile without new work')
        return
    if not isinstance(ref, dict) or ref.get('phase') != 'configured':
        raise RoomError('Routing adoption is pending; reconcile its exact intent before new work')
    if set(pending) != {_path(directory, ref.get('receipt', ''))}:
        raise RoomError('Unclaimed or missing routing-adoption intent evidence')


class _ReadOnly:
    def __init__(self, service):
        self.service = service

    def __getattr__(self, name):
        return getattr(self.service, name)

    def record_reroute(self, *args):
        raise RoomError('Observed model reroute refuses adoption; audit never mutates the room')


def _native(service, state):
    from ao_provider_transition import _CompleteClient, _native as native_evidence
    client, result = _CompleteClient(service.client(state)), {}
    directory = service.root / 'rooms' / state['room_id']
    for role in ('engineer', 'reviewer'):
        binding = state['bindings'][role]
        snapshot = ao.Service.identity(_ReadOnly(service), client, state, binding)
        if role == 'engineer' and snapshot.get('controller') != 'stopped':
            raise RoomError('Stop the idle engineer through AO and record it before routing adoption')
        # Share the complete raw-history and immutable-receipt contract used by
        # the provider audit. Retained completed pre-binding turns are evidence,
        # never permission to replay them or erase their history.
        result[role] = native_evidence(state, binding, snapshot, directory)
    return result


def _project(service, state):
    raw = service.client(state).request('GET', '/projects/' + state['ao_project_id'])
    value = raw.get('project', raw)
    if value.get('id', value.get('projectId')) != state['ao_project_id'] or not isinstance(value.get('config'), dict):
        raise RoomError('AO project configuration identity is unavailable')
    config = value['config']
    if not isinstance(config.get('agentRules'), str) or config.get('agentRulesFile'):
        raise RoomError('Routing adoption requires explicit inline project rules; file-based rules need separate handling')
    for path in (service.root / 'rooms').glob('*/state.json'):
        other = json.loads(owned_bytes(path))
        if other['room_id'] != state['room_id'] and other.get('ao_project_id') == state['ao_project_id']:
            raise RoomError('Another room shares these AO project rules; refuse a cross-room change')
    return copy.deepcopy(config)


def _ledger(service, directory, state, epoch):
    import ao_provider_transition
    import deepseek_adapter
    ao_delegates.assert_settled(service.root.parent, copy.deepcopy(state), directory)
    present, rows = ao_provider_transition._ledger_rows(service.root.parent, state['room_id'])
    if not present:
        raise RoomError('Retained routing adoption requires an existing delegate ledger; never recreate missing history at MCP startup')
    ao_provider_transition._ledger_check(rows)
    original = ao_provider_transition._saved_audit(directory, epoch['audit_sha256'])['evidence']['delegate']['record']
    profiles = {}
    for delegate in (original, state['delegate']):
        ao_delegates.validate_provider(directory, {**state, 'delegate': delegate})
        inventory = delegate['inventory']
        identity = ao_provider_transition._inventory_profiles(inventory)
        config = deepseek_adapter.validate_config(json.loads(owned_bytes(identity['config_path'])), service.root.parent)
        profiles[identity['profile_sha256']] = (inventory['export_dir'], config)
    for row in rows:
        if row['profile_sha256'] == epoch['target']['profile_sha256']:
            raise RoomError('Routing adoption must precede every target-provider ledger job')
        if row['profile_sha256'] not in profiles:
            raise RoomError('Delegate ledger contains an unrecognized provider epoch')
        exports, config = profiles[row['profile_sha256']]
        if row['requested_model'] != config['model']:
            raise RoomError('Delegate ledger model contradicts its provider epoch')
        if row['state'] == deepseek_adapter.COMPLETED:
            ao_delegates.verify_content(service.root.parent, state['room_id'], exports, row, config['max_content_bytes'])
    return {'present': present, 'rows': rows, 'probes': probe_evidence(service.root.parent, state['room_id'])}


def _inspect(service, directory, state):
    import ao_provider_transition
    if state.get('routing_adoption') or list((directory / BASE / 'requests').glob('*.json')):
        raise RoomError('Routing adoption is already used or pending')
    provider = ao_provider_transition.validate(service, state)
    if provider is None:
        raise RoomError('Commit the supported official-provider amendment before routing adoption')
    record, epoch = provider
    if any(r.get('provider_epoch') == 2 for r in state['requests'].values()):
        raise RoomError('Routing adoption must precede every new provider-epoch request')
    if (directory / 'delegate-launch.json').exists() or (directory / 'delegate-launch.json').is_symlink():
        raise RoomError('Routing adoption must precede the first epoch-2 native launch; preserve any unexpected launch evidence')
    if (epoch['target']['backend'], epoch['target']['model']) != ('official', 'deepseek-flash'):
        raise RoomError('Routing adoption requires the approved official provider target')
    ledger = _ledger(service, directory, state, epoch)
    if any(v['state'] == 'running' for v in state['verifications']):
        raise RoomError('Unfinished verification refuses routing adoption')
    prepared = ao_delegates.validate_preparation(directory, state, state['bindings']['engineer']['session_id'])
    if not isinstance(prepared.get('routing'), dict) or prepared['routing'].get('version') != 1:
        raise RoomError('Only a historical v1 preparation is eligible')
    launcher_compatibility(service, state, prepared)
    native = _native(service, state)
    actual = ao_workflow.workspace(service, directory, state)
    config = _project(service, state)
    if not ao_routing.rules_match(ao_routing.observe_rules(service.client(state), state), prepared['routing']['rules']):
        raise RoomError('AO project rules drifted from the original preparation')
    files = {rel: owned_bytes(actual / rel).decode('utf-8') for rel in ao_routing.FILES}
    # Every writable runtime target is validated before any future mutation.
    for rel in ao_routing.FILES:
        ao_routing._target(actual, rel)
        ao_routing._require_ignored(actual, rel)
    return {'room_id': state['room_id'], 'state': copy.deepcopy(state), 'state_sha256': ao.digest(state),
            'provider_epoch_sha256': state['provider_transition']['epoch_sha256'], 'provider_epoch': epoch,
            'prepared': prepared, 'native': native, 'candidate': candidate_snapshot(actual), 'ledger': ledger,
            'original_config': config, 'runtime_files': files,
            'guard_sha256': ao.digest(owned_bytes(Path(__file__).with_name('ao_routing_guard.py'))),
            'wrapper_sha256': ao.digest(owned_bytes(Path(__file__).with_name('ao_mcp_attachment.py')))}


def audit(service, room_id):
    with service.locked(room_id) as (directory, state):
        evidence = _inspect(service, directory, state)
        value = {'evidence': evidence, 'attachment_id': uuid.uuid4().hex, 'recorded_at': time.time()}
        if len(_json(value)) > MAX_EVIDENCE:
            raise RoomError('Routing-adoption audit exceeds the 96 MB evidence bound; no audit was written')
        sha = ao.digest(value)
        _store_once(directory / BASE / 'audits' / (sha + '.json'), value)
        return {'eligible': True, 'audit_sha256': sha, 'room_id': room_id,
                'source_routing': 1, 'target_routing': 2, 'model_requests': 0,
                'meaning': 'Read-only stopped-controller audit; no native work or project configuration was changed'}


def _bundle(directory, audit):
    e = audit['evidence']
    prepared = copy.deepcopy(e['prepared'])
    runtime = copy.deepcopy(e['runtime_files'])
    guard = directory / REL / 'ao_routing_guard.py'
    wrapper = directory / REL / 'ao_mcp_attachment.py'
    manifest_path = directory / REL / 'mcp-attachment.json'
    old = prepared['routing']
    settings = json.loads(runtime['.claude/settings.local.json'])
    entry = {'matcher': old['matcher'], 'hooks': [{'type': 'command', 'command': old['hook_command'], 'timeout': 30}]}
    hooks = settings['hooks']['PreToolUse']
    if hooks.count(entry) != 1:
        raise RoomError('Original managed hook is missing or duplicated; do not weaken other hooks')
    settings['hooks']['PreToolUse'] = [h for h in hooks if h != entry]
    command = ao_routing.hook_command(old['python'], guard)
    settings = ao_routing.settings_document(settings, command)
    runtime['.claude/settings.local.json'] = _json(settings).decode()
    new_config = copy.deepcopy(e['original_config'])
    new_config['agentRules'] += '\n\n' + INSTRUCTION
    rules = copy.deepcopy(old['rules'])
    rules['agent_rules_sha256'] = ao.digest(new_config['agentRules'].encode())
    prepared['routing'] = {**old, 'version': 2, 'execution_policy': ao_routing.EXECUTION_POLICY,
        'guard_path': str(guard), 'guard_sha256': e['guard_sha256'], 'hook_command': command,
        'matcher': ao_routing.MATCHER, 'rules': rules,
        'files': {rel: ao.digest(text.encode()) for rel, text in runtime.items()}}
    delegate = e['state']['delegate']
    profiles = [p for p in delegate['files'] if Path(p).name == 'implementation-mcp.json']
    if len(profiles) != 1:
        raise RoomError('Pinned MCP profile identity is ambiguous')
    manifest = {'version': 1, 'attachment_id': audit['attachment_id'], 'room_directory': str(directory),
        'room_id': e['room_id'], 'session_id': e['state']['bindings']['engineer']['session_id'],
        'worktree': prepared['worktree'], 'child_server': copy.deepcopy(prepared['server']),
        'provider_files': {**delegate['files'], **delegate['inventory']['files']}, 'profile_path': profiles[0],
        'wrapper_path': str(wrapper), 'wrapper_sha256': e['wrapper_sha256'], 'expected_tools': TOOLS,
        'receipt_directory': str(directory / BASE / 'connections' / audit['attachment_id'])}
    prepared['mcp_attachment'] = {'version': 1, 'attachment_id': audit['attachment_id'],
        'manifest_path': str(manifest_path), 'manifest_sha256': ao.digest(manifest)}
    prepared['server'] = {'command': manifest['child_server']['command'],
        'args': [str(wrapper), '--manifest', str(manifest_path)], 'env': {'PROJECT_ROOM_WORKTREE': prepared['worktree']}}
    files = {REL + '/preparation.json': _json(prepared), REL + '/mcp-attachment.json': _json(manifest),
             REL + '/project-config.json': _json({'config': new_config}),
             REL + '/ao_routing_guard.py': owned_bytes(Path(__file__).with_name('ao_routing_guard.py')),
             REL + '/ao_mcp_attachment.py': owned_bytes(Path(__file__).with_name('ao_mcp_attachment.py'))}
    if ao.digest(files[REL + '/ao_routing_guard.py']) != e['guard_sha256'] or ao.digest(files[REL + '/ao_mcp_attachment.py']) != e['wrapper_sha256']:
        raise RoomError('Installed guard or attachment wrapper changed after audit')
    for rel, text in e['runtime_files'].items():
        files[BASE + '/original-runtime/' + rel] = text.encode()
    return prepared, runtime, new_config, files


def _place(path, data):
    if path.exists() or path.is_symlink():
        if owned_bytes(path) != data:
            raise RoomError('Staged routing-adoption file changed')
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # The existing append-only writer supplies fsync/no-follow/0600 semantics.
    from ao_provider_transition import _place as place
    place(path, data)


def _pending(service, directory, state, reference):
    if set((directory / BASE / 'requests').glob('*.json')) != {_path(directory, reference['receipt'])}:
        raise RoomError('Ambiguous routing-adoption intent evidence')
    record = _load(directory, reference['receipt'], reference['receipt_sha256'])
    if (record['room_id'] != state['room_id'] or record['inputs']['request_id'] != reference['request_id']
            or reference['key'] != ao.digest(record['inputs'])):
        raise RoomError('Routing-adoption intent identity changed')
    audit = _audit(directory, record['inputs']['audit_sha256'])
    if ao.digest(audit['evidence']['state']) != record['before_state_sha256']:
        raise RoomError('Routing-adoption audit chain changed')
    original = copy.deepcopy(state)
    original.pop('routing_adoption', None)
    if original != audit['evidence']['state']:
        raise RoomError('Room changed during pending routing adoption')
    if (directory / 'delegate-launch.json').exists() or (directory / 'delegate-launch.json').is_symlink():
        raise RoomError('Native MCP launch occurred during pending routing adoption')
    import ao_provider_transition
    base = ao_provider_transition.validate(service, state, allow_pending=True)
    if base is None or base[1] != audit['evidence']['provider_epoch']:
        raise RoomError('Provider epoch changed during pending routing adoption')
    ao_delegates.validate_preparation(directory, state, state['bindings']['engineer']['session_id'], check_routing=False)
    old_guard = audit['evidence']['prepared']['routing']
    if ao.digest(owned_bytes(old_guard['guard_path'])) != old_guard['guard_sha256']:
        raise RoomError('Original routing guard changed during adoption')
    if _native(service, state) != audit['evidence']['native']:
        raise RoomError('Native history changed during routing adoption')
    if _ledger(service, directory, state, base[1]) != audit['evidence']['ledger']:
        raise RoomError('Delegate ledger changed during routing adoption')
    if candidate_snapshot(Path(audit['evidence']['prepared']['worktree'])) != audit['evidence']['candidate']:
        raise RoomError('Candidate changed during routing adoption')
    return record, audit


def _record(room_id, inputs, state, prepared, runtime, files):
    return {'room_id': room_id, 'inputs': inputs, 'before_state_sha256': ao.digest(state),
            'target_preparation_sha256': ao.digest(prepared),
            'manifest': {p: ao.digest(b) for p, b in files.items()},
            'runtime': {p: ao.digest(t.encode()) for p, t in runtime.items()}}


def _configured_state(state, prepared, observed, observed_at):
    """Build the exact saved state before its size is checked or any pointer commits."""
    result = copy.deepcopy(state)
    relative = 'routing/observations/' + ao.digest(observed)[:16] + '.json'
    result.update(preparation=REL + '/preparation.json', preparation_sha256=ao.digest(prepared))
    result['routing_adoption']['phase'] = 'configured'
    result['routing_rules'] = {'observed_at': observed_at, 'source': 'routing-adoption', 'consistent': True,
        'evidence': relative, 'evidence_sha256': ao.digest(observed),
        'clause_present': observed['clause_present'], 'agent_rules_sha256': observed['agent_rules_sha256']}
    return result


def stage(service, room_id, audit_sha256, authorization, diagnosis, request_id):
    ao.identifier(request_id)
    ao.nonempty(authorization, 'authorization', 6000)
    ao.nonempty(diagnosis, 'diagnosis', 6000)
    inputs = {'audit_sha256': audit_sha256, 'authorization': authorization, 'diagnosis': diagnosis, 'request_id': request_id}
    with service.locked(room_id) as (directory, state):
        ref = state.get('routing_adoption')
        if ref:
            if ref.get('request_id') != request_id or ref.get('key') != ao.digest(inputs):
                raise RoomError('Routing adoption already belongs to another request')
            if ref.get('phase') == 'configured':
                validate(service, state)
                return _result(directory, state, ref)
            record, saved = _pending(service, directory, state, ref)
        else:
            saved = _audit(directory, audit_sha256)
            prepared, runtime, config, files = _bundle(directory, saved)
            record = _record(room_id, inputs, state, prepared, runtime, files)
            relative = BASE + '/requests/' + request_id + '.json'
            ref = {'phase': 'pending', 'request_id': request_id, 'key': ao.digest(inputs),
                   'receipt': relative, 'receipt_sha256': ao.digest(record)}
            proposed = {**state, 'routing_adoption': ref}
            launcher_compatibility(service, proposed, prepared)
            # Routing observation fields have fixed shapes/digest lengths. Leave
            # more than the maximum JSON float width for the later real timestamp.
            final = _configured_state(proposed, prepared, prepared['routing']['rules'], 0)
            launcher_compatibility(service, final, prepared, reserve_bytes=64)
            if (directory / relative).exists():
                # The process may have stopped after persisting the immutable
                # intent but before saving its state pointer. Reconcile only
                # those exact inputs and the still-unchanged original state.
                _pending(service, directory, state, ref)
            else:
                if _inspect(service, directory, state) != saved['evidence']:
                    raise RoomError('Routing-adoption audit is stale')
                _store_once(directory / relative, record)
            state['routing_adoption'] = ref
            service.save(directory, state)  # Durable intent precedes every runtime write.
        prepared, runtime, config, files = _bundle(directory, saved)
        if record != _record(room_id, inputs, saved['evidence']['state'], prepared, runtime, files):
            raise RoomError('Staged routing-adoption bytes changed')
        current_config = _project(service, state)
        if current_config not in (saved['evidence']['original_config'], config):
            raise RoomError('Concurrent project configuration change stops routing adoption')
        worktree = Path(prepared['worktree'])
        for rel, text in runtime.items():
            current = owned_bytes(worktree / rel)
            if current not in (saved['evidence']['runtime_files'][rel].encode(), text.encode()):
                raise RoomError('Runtime file changed during routing adoption: ' + rel)
        for relative, data in files.items():
            _place(directory / relative, data)
        # The witness must not create evidence directories before provenance
        # checks. Prepare its private destination under the saved adoption intent.
        from ao_mcp_attachment import _path as attachment_path, _private_directory
        connection_root = attachment_path(str(directory / BASE / 'connections'))
        connection_root.mkdir(exist_ok=True, mode=0o700)
        _private_directory(connection_root)
        receipts = attachment_path(str(connection_root / saved['attachment_id']))
        receipts.mkdir(exist_ok=True, mode=0o700)
        _private_directory(receipts)
        for rel, text in runtime.items():
            data = text.encode()
            if owned_bytes(worktree / rel) != data:
                ao_routing._require_ignored(worktree, rel)
                ao_routing._write(worktree, rel, data, saved['evidence']['runtime_files'][rel].encode())
        return _result(directory, state, ref)


def _result(directory, state, ref):
    return {'room_id': state['room_id'], 'request_id': ref['request_id'], 'phase': ref['phase'],
            'project_config_payload': str(directory / REL / 'project-config.json'),
            'project_config_path': '/projects/' + state['ao_project_id'] + '/config',
            'model_requests': 0, 'meaning': 'Operator applies the exact staged AO config while stopped; activate only after matching observations'}


def activate(service, room_id, request_id):
    ao.identifier(request_id)
    with service.locked(room_id) as (directory, state):
        ref = state.get('routing_adoption')
        if not ref or ref.get('request_id') != request_id:
            raise RoomError('Use the exact pending routing-adoption request')
        if ref['phase'] == 'configured':
            validate(service, state)
            return _result(directory, state, ref)
        record, saved = _pending(service, directory, state, ref)
        prepared, runtime, config, files = _bundle(directory, saved)
        if record != _record(room_id, record['inputs'], saved['evidence']['state'], prepared, runtime, files):
            raise RoomError('Staged routing-adoption intent changed')
        if _project(service, state) != config:
            raise RoomError('The exact staged AO project configuration has not been observed')
        for path, expected in record['manifest'].items():
            if ao.digest(owned_bytes(_path(directory, path))) != expected:
                raise RoomError('Staged routing evidence changed')
        ao_routing.validate_local(prepared)
        observed_rules = ao_routing.observe_rules(service.client(state), state)
        if not ao_routing.rules_match(observed_rules, prepared['routing']['rules']):
            raise RoomError('New rules do not match the staged preparation')
        # Recheck native ownership immediately before committing active pointers.
        _pending(service, directory, state, ref)
        configured = _configured_state(state, prepared, observed_rules, time.time())
        launcher_compatibility(service, configured, prepared)
        observation_path = directory / configured['routing_rules']['evidence']
        if observation_path.exists() or observation_path.is_symlink():
            if json.loads(owned_bytes(observation_path)) != observed_rules:
                raise RoomError('Saved routing observation evidence was modified; preserve it for diagnosis')
        else:
            _store_once(observation_path, observed_rules)
        service.save(directory, configured)
        validate(service, configured)
        return _result(directory, configured, configured['routing_adoption'])


def validate(service, state, provider_epoch=None):
    directory = service.root / 'rooms' / state['room_id']
    pending_gate(service, state)
    ref = state.get('routing_adoption')
    if ref is None:
        return None
    try:
        record = _load(directory, ref['receipt'], ref['receipt_sha256'])
        if ref['key'] != ao.digest(record['inputs']) or record['inputs']['request_id'] != ref['request_id'] or record['room_id'] != state['room_id']:
            raise RoomError('Routing-adoption reference changed')
        saved = _audit(directory, record['inputs']['audit_sha256'])
        before = saved['evidence']['state']
        if ao.digest(before) != record['before_state_sha256']:
            raise RoomError('Routing-adoption audit chain changed')
        epoch = provider_epoch or saved['evidence']['provider_epoch']
        if (before['preparation'] != epoch['target']['preparation']
                or before['preparation_sha256'] != epoch['target']['preparation_sha256']
                or ao.digest(state['delegate']) != epoch['target']['delegate_sha256']):
            raise RoomError('Routing adoption does not extend the exact committed provider preparation')
        if (state['preparation'] != REL + '/preparation.json'
                or state['preparation_sha256'] != record['target_preparation_sha256']):
            raise RoomError('Routing adoption active identity changed')
        from ao_provider_transition import validate_bindings
        validate_bindings(service, state, before['bindings'])
        for relative, expected in record['manifest'].items():
            if ao.digest(owned_bytes(_path(directory, relative))) != expected:
                raise RoomError('Immutable routing-adoption evidence changed')
        prepared = json.loads(owned_bytes(directory / state['preparation']))
        if ao.digest(prepared) != state['preparation_sha256']:
            raise RoomError('Routing-adoption preparation changed')
        ao_routing.validate_local(prepared)
        return record
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RoomError('Routing-adoption evidence is unreadable or inconsistent') from exc
