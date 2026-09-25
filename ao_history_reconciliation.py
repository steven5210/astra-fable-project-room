"""Audited evidence for completed receipts captured with truncated AO history.

No receipt is rewritten. Only an explicit outcome audit can establish or renew
this proof; ordinary observations can validate it or durably invalidate it.
"""
import re

from room import RoomError

PROOF = 'history_reconciliation_sha256'
INVALIDATION = 'history_reconciliation_invalidation_sha256'


class StrictHistoryClient:
    """Use the existing strict reader for this lane without changing its bounds."""
    def __init__(self, client):
        self.client = client

    def request(self, method, path, payload=None):
        if method != 'GET' or payload is not None:
            raise RoomError('History reconciliation observations allow only body-free GETs')
        return self.client.request(method, path, payload)

    def conversation(self, session_id):
        from ao_history import conversation
        from ao_project_room import identifier
        return conversation(self.request, '/sessions/' + identifier(session_id) + '/conversation', strict=True)


def requires_strict_history(directory, state, binding):
    from ao_outcomes import latest_for_role
    from ao_workflow import completed_receipt
    for role in ('engineer', 'reviewer'):
        try:
            request = latest_for_role(state, role)
        except (KeyError, TypeError, ValueError):
            # Malformed ordering must not mask an unrelated preexisting guard.
            # Any proof on this binding still forces strict evidence observation.
            if any(isinstance(r, dict) and r.get(PROOF) and r.get('session_id') == binding.get('session_id')
                   for r in state['requests'].values()):
                return True
            continue
        if not request or request.get('session_id') != binding.get('session_id'):
            continue
        if request.get(PROOF):
            return True
        if request.get('state') == 'completed' and request.get('receipt') and not request.get('model_reroute'):
            try:
                return completed_receipt(directory, request).get('history_truncated') is True
            except (RoomError, OSError, ValueError, KeyError, TypeError):
                # Selection is not receipt admission. Preserve unrelated operations
                # before any proof exists; outcome audit still validates the receipt.
                return False
    return False


def _record(directory, area, request_id, sha):
    from ao_project_room import digest, read
    if not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{64}', sha):
        raise RoomError('Malformed history reconciliation evidence identity')
    path = directory / area / request_id / (sha + '.json')
    if any(p.is_symlink() for p in (path, path.parent, path.parent.parent)):
        raise RoomError('History reconciliation evidence is symlinked')
    value = read(path)
    if (not isinstance(value, dict) or digest(value) != sha or value.get('version') != 1
            or value.get('room_id') != directory.name or value.get('request_id') != request_id):
        raise RoomError('History reconciliation evidence changed or belongs to another request')
    return value


def _write(directory, area, value):
    from ao_project_room import atomic, digest
    sha = digest(value)
    path = directory / area / value['request_id'] / (sha + '.json')
    if path.exists() or path.is_symlink():
        if _record(directory, area, value['request_id'], sha) != value:
            raise RoomError('Conflicting history reconciliation evidence')
    else:
        if path.parent.is_symlink() or path.parent.parent.is_symlink():
            raise RoomError('History reconciliation evidence is symlinked')
        atomic(path, value)
    return sha


def invalidation_head(directory, request):
    head = request.get(INVALIDATION)
    cursor, seen = head, set()
    while cursor is not None:
        if cursor in seen or len(seen) >= 1000:
            raise RoomError('History reconciliation invalidation chain is cyclic or excessive')
        seen.add(cursor)
        row = _record(directory, 'history-reconciliation-invalidations', request['request_id'], cursor)
        if row.get('kind') != 'invalidation':
            raise RoomError('Malformed history reconciliation invalidation')
        cursor = row.get('previous_sha256')
    return head


def load_proof(directory, request):
    sha = request.get(PROOF)
    if sha is None:
        return None
    value = _record(directory, 'history-reconciliations', request['request_id'], sha)
    if (value.get('kind') != 'complete_history_native_quota'
            or value.get('receipt_sha256') != request.get('receipt_sha256')
            or value.get('text_sha256') != request.get('text_sha256')):
        raise RoomError('History reconciliation proof no longer binds the original receipt')
    invalidation_head(directory, request)
    return value


def invalidate(service, directory, state, request_id, reason):
    """Persist only this invalidation, never other uncommitted operation changes."""
    from ao_project_room import read
    saved_state = read(directory / 'state.json')
    request = saved_state['requests'].get(request_id, {})
    if not request.get(PROOF):
        return
    previous = request.get(INVALIDATION)
    if previous:
        try:
            latest = _record(directory, 'history-reconciliation-invalidations', request_id, previous)
        except (RoomError, OSError, ValueError):
            latest = None  # Preserve the damaged ancestor and record the new observation.
        if latest and latest.get('proof_sha256') == request[PROOF]:
            state['requests'][request_id][INVALIDATION] = previous
            return
    value = {'version': 1, 'kind': 'invalidation', 'room_id': state['room_id'],
             'request_id': request_id, 'proof_sha256': request[PROOF],
             'outcome_sha256': request.get('semantic_outcome_sha256'),
             'previous_sha256': previous, 'reason': str(reason)[:500]}
    sha = _write(directory, 'history-reconciliation-invalidations', value)
    request[INVALIDATION] = sha
    service.save(directory, saved_state)
    state['requests'][request_id][INVALIDATION] = sha


def invalidate_latest(service, directory, state, reason):
    """A failed locked operation cannot let an established proof silently revive.

Also covers strict-reader/identity refusals before observe() can be entered.
Conservative: even an invalid operator command requires an explicit re-audit.
"""
    from ao_outcomes import latest_for_role
    for role in ('engineer', 'reviewer'):
        proven = [r for r in state['requests'].values()
                  if isinstance(r, dict) and r.get('role') == role and r.get(PROOF)]
        if not proven:
            continue
        try:
            latest = latest_for_role(state, role)
            candidates = [latest] if latest.get(PROOF) else []
        except (KeyError, TypeError, ValueError):
            # Unknown ordering cannot safely identify a latest proof. Invalidate
            # every proof on this role rather than let restoration revive one.
            candidates = proven
        for request in candidates:
            invalidate(service, directory, state, request['request_id'], reason)


def _inputs(state, request, saved, observed, snapshot, native):
    from ao_project_room import busy, conflicting_reroute, digest
    from ao_native_outcome import source_keys
    from ao_outcomes import classify, _unowned_context_turns
    if (request.get('state') != 'completed' or saved.get('history_truncated') is not True
            or not isinstance(request.get('provider_turn_id'), str) or not request['provider_turn_id'].strip()
            or (saved.get('turn') or {}).get('state') != 'completed'
            or observed['turn'].get('state') != 'completed' or snapshot.get('history_truncated') is not False
            or busy(snapshot) or not isinstance(native, dict) or native.get('unknown')
            or not isinstance(native.get('anchor_uuid'), str) or not native['anchor_uuid']
            or native.get('next_human_uuid') is not None or conflicting_reroute(request, snapshot)):
        return None
    settings = saved.get('settings')
    if (not isinstance(settings, dict) or settings.get('model') != request.get('model')
            or settings.get('reasoningEffort') != request.get('reasoning_effort')
            or saved['turn'].get('id') != request.get('turn_id')
            or saved['turn'].get('providerTurnId') != request.get('provider_turn_id')
            or saved.get('modelReroute')):
        return None
    source = native.get('source')
    source_key, _ = source_keys(request['role'])
    errors = native.get('errors')
    if (not isinstance(source, dict) or source != state.get(source_key)
            or source.get('session_id') != request['session_id']
            or not isinstance(native.get('source_sha256'), str)
            or not re.fullmatch('[0-9a-f]{64}', native['source_sha256'])
            or not isinstance(errors, list) or any(not isinstance(e, dict) for e in errors)
            or not any(e.get('error') == 'rate_limit' or e.get('http_status') == 429 for e in errors)):
        return None
    baseline_turns = set(request['baseline']['turn_ids'])
    current_turns = {turn['id'] for turn in snapshot['turns']}
    proven_context = _unowned_context_turns(state, {
        item['turn_id'] for field in ('compaction_imports', 'task_notification_imports')
        for item in native.get(field, [])})
    if (not baseline_turns <= current_turns
            or current_turns - baseline_turns - {request['turn_id']} - proven_context):
        return None
    # Validate every other saved-result field with the unchanged classifier.
    # This derived view never replaces the original receipt or its usage.
    if (classify({**saved, 'history_truncated': False}, native)['kind'] != 'quota_limit'
            or classify({**observed, 'history_truncated': False}, native)['kind'] != 'quota_limit'):
        return None
    return {'session_id': request['session_id'], 'role': request['role'],
            'turn_id': request['turn_id'], 'provider_turn_id': request['provider_turn_id'],
            'conversation_id': snapshot.get('conversationId'), 'branch_id': snapshot.get('activeBranchId'),
            'baseline_sha256': digest(request['baseline']), 'messages_sha256': digest(saved['messages']),
            'turns_sha256': digest(snapshot['turns']), 'ao_terminal_sha256': digest(observed['turn']),
            'provider_failures_sha256': digest(observed['provider_failures']),
            'session_failures_sha256': digest(observed['sessionFailures']), 'native': native}


def reconcile(service, directory, state, request, saved, observed, snapshot, native, explicit_audit):
    """Return the exact proof digest only when the current evidence authorizes it."""
    from ao_workflow import normal
    import ao_delegates
    if saved.get('history_truncated') is not True:
        return None
    inputs = _inputs(state, request, saved, observed, snapshot, native)
    prior = load_proof(directory, request)
    head = invalidation_head(directory, request)
    if prior and (inputs is None or prior.get('inputs') != inputs or prior.get('invalidation_sha256') != head):
        invalidate(service, directory, state, request['request_id'], 'Complete-history reconciliation evidence changed or became unknown')
        head = invalidation_head(directory, request)
    elif prior and inputs is not None:
        if normal(state) or 'delegate' in state:
            ao_delegates.assert_settled(service.root.parent, state, directory)
        return request[PROOF]
    if not explicit_audit or inputs is None:
        return None
    if normal(state) or 'delegate' in state:
        ao_delegates.assert_settled(service.root.parent, state, directory)
    value = {'version': 1, 'kind': 'complete_history_native_quota', 'room_id': state['room_id'],
             'request_id': request['request_id'], 'receipt_sha256': request['receipt_sha256'],
             'text_sha256': request['text_sha256'], 'saved_history_truncated': True,
             'inputs': inputs, 'invalidation_sha256': head,
             'supersedes_outcome_sha256': request.get('semantic_outcome_sha256')}
    sha = _write(directory, 'history-reconciliations', value)
    request[PROOF] = sha
    return sha
