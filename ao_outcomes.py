"""Durable semantic holds, separate from AO transport and immutable receipts."""
import json

from room import RoomError

QUOTA_KINDS = {'rate_limit', 'quota_limit', 'quota_exhausted', 'usage_limit', 'budget_exhausted'}
RESUMABLE = {'quota_limit', 'provider_error', 'output_truncated'}


def activity_failures(snapshot, turn_id):
    """AO 0.13 normalizes ACP failures into typed system activities, not model prose."""
    result = []
    activities = snapshot.get('activities', [])
    if not isinstance(activities, list) or any(not isinstance(a, dict) for a in activities):
        raise RoomError('Malformed AO activity evidence')
    for item in activities:
        if item.get('turnId') != turn_id or item.get('activityKind') != 'system':
            continue
        detail = item.get('detail')
        if isinstance(detail, str):
            try:
                detail = json.loads(detail)
            except ValueError:
                continue
        if not isinstance(detail, dict) or detail.get('event') != 'provider.failure':
            continue
        # A retry warning settled by a later substantive response is historical.
        if detail.get('severity') == 'warning' and item.get('status') == 'completed':
            continue
        result.append({'activity_id': item.get('id'), 'turn_id': turn_id,
                       'category': detail.get('category'), 'severity': detail.get('severity'),
                       'status': item.get('status'), 'type': 'provider_failure'})
    return result


def classify(receipt, native=None, require_structured=True):
    """Only typed/correlated errors establish provider failure; prose is not an API signal."""
    if not isinstance(receipt, dict):
        return {'kind': 'unknown', 'hold': True, 'reason': 'Malformed native receipt'}
    turn = receipt.get('turn')
    messages = receipt.get('messages')
    if (not isinstance(turn, dict) or not isinstance(messages, list) or receipt.get('history_truncated')
            or any(not isinstance(m, dict) or not isinstance(m.get('text', ''), str) for m in messages)):
        return {'kind': 'unknown', 'hold': True, 'reason': 'Incomplete or malformed native result'}
    errors, stops = [], []
    for key in ('error', 'failure'):
        value = turn.get(key)
        if value:
            if isinstance(value, dict):
                errors.append(value)
            else:
                errors.append({'type': 'unclassified_error'})
    if turn.get('errorMessage'):
        errors.append({'type': 'unclassified_error'})
    provider_failures = receipt.get('provider_failures', [])
    if not isinstance(provider_failures, list) or any(not isinstance(x, dict) for x in provider_failures):
        return {'kind': 'unknown', 'hold': True, 'reason': 'Malformed provider activity evidence'}
    errors += provider_failures
    for key in ('stopReason', 'stop_reason'):
        if turn.get(key):
            stops.append(turn[key])
    failures = receipt.get('sessionFailures', [])
    if not isinstance(failures, list):
        return {'kind': 'unknown', 'hold': True, 'reason': 'Malformed typed failure evidence'}
    for failure in failures:
        if not isinstance(failure, dict):
            return {'kind': 'unknown', 'hold': True, 'reason': 'Malformed typed failure evidence'}
        if (failure.get('turnId') == turn.get('id') or
                (failure.get('providerTurnId') and failure['providerTurnId'] == turn.get('providerTurnId'))):
            errors.append(failure)
        elif not failure.get('turnId') and not failure.get('providerTurnId'):
            return {'kind': 'unknown', 'hold': True, 'reason': 'Failure lacks native turn attribution'}
    if native and not native.get('unknown'):
        errors += native['errors']
        stops += native['stop_reasons']
    quota = any((isinstance(kind := (e.get('type') or e.get('kind') or e.get('error')), str) and kind in QUOTA_KINDS)
                or e.get('httpStatus', e.get('http_status')) == 429 for e in errors)
    if errors:
        return {'kind': 'quota_limit' if quota else 'provider_error', 'hold': True,
                'reason': 'Correlated native provider failure takes precedence over output/transport completion'}
    if native and native.get('unknown'):
        return {'kind': 'unknown', 'hold': True, 'reason': native['unknown']}
    if 'max_tokens' in stops:
        return {'kind': 'output_truncated', 'hold': True, 'reason': 'Native output was truncated; inspect partial work before continuation'}
    finals = [m for m in messages if m.get('role') == 'assistant' and not m.get('streaming') and m.get('text', '').strip()]
    if not finals:
        return {'kind': 'unknown', 'hold': True, 'reason': 'Native result has no final response; absence is not successful completion or a quota diagnosis'}
    from ao_response_normalization import strict_object, ResponseFormatError
    try:
        strict_object(finals[-1]['text'])
        structured = True
    except ResponseFormatError:
        structured = False
    # An explicit native end_turn can establish a completed formatting error.
    # Otherwise a prose/partial result might hide a bridge failure and requires diagnosis.
    if require_structured and not structured and (not stops or stops[-1] != 'end_turn'):
        return {'kind': 'unknown', 'hold': True, 'reason': 'Unstructured final lacks an established native end_turn'}
    return {'kind': 'final_available', 'hold': False, 'reason': 'Final observed; semantic report and acceptance checks remain separate'}


def receipt(directory, request):
    if request.get('state') == 'uncertain' and (request.get('observed_turn') or {}).get('state') == 'failed':
        from ao_project_room import digest, read, sent_message
        value = read(directory / request['receipt'])
        turn = value.get('turn') or {}
        if (digest(value) != request['receipt_sha256'] or turn.get('state') != 'failed'
                or not request.get('provider_turn_id') or turn.get('providerTurnId') != request['provider_turn_id']
                or turn.get('id') != request.get('turn_id') or value.get('history_truncated')
                or not sent_message(request, value) or request.get('model_reroute')):
            raise RoomError('Failed-turn evidence is incomplete or changed')
        return value  # Evidence for audit only; this does not grant continuation.
    from ao_workflow import completed_receipt
    return completed_receipt(directory, request)


def native_quota_failure(record):
    native = record.get('native') or {}
    return (record['outcome']['kind'] == 'quota_limit' and not native.get('unknown')
            and bool(native.get('anchor_uuid')) and native.get('next_human_uuid') is None
            and any(e.get('error') == 'rate_limit' or e.get('http_status') == 429 for e in native.get('errors', [])))


def validate_settlement(directory, request):
    from ao_project_room import digest, read
    release = release_record(directory, {'room_id': directory.name}, request)
    if not release:
        raise RoomError('Settled native failure lacks its audited continuation')
    proof = release.get('native_failure_settlement') or {}
    evidence_path = 'outcomes/' + request['request_id'] + '/' + release.get('outcome_sha256', '') + '.json'
    record = read(directory / evidence_path)
    if (digest(release) != request.get('outcome_resume_sha256')
            or release.get('room_id') != directory.name or release.get('blocked_request_id') != request['request_id']
            or digest(record) != release.get('outcome_sha256') or not native_quota_failure(record)
            or record['receipt_sha256'] != request['receipt_sha256']
            or proof != {'prior_state': 'uncertain', 'ao_state': 'failed', 'receipt_sha256': request['receipt_sha256']}
            or (request.get('observed_turn') or {}).get('state') != 'failed'):
        raise RoomError('Settled native failure proof changed')


def audit_quiet(service, directory, state, request):
    failed = request.get('state') == 'uncertain' and (request.get('observed_turn') or {}).get('state') == 'failed'
    if failed:
        receipt(directory, request)
    service.quiet(state, outcome_request_id=request['request_id'] if failed else None)


def latest_for_role(state, role):
    rows = [r for r in state['requests'].values() if r['role'] == role]
    return max(rows, key=lambda r: r['created_order']) if rows else None


def observe(service, directory, state, request, snapshot, allow_unknown_clear=False):
    from ao_project_room import atomic, digest, read, sent_message, turn_ids
    from ao_project_room import native_turn_identity
    identity = native_turn_identity(request)
    if identity and not request.get('provider_turn_id'):
        request['provider_turn_id'] = identity
    saved = receipt(directory, request)
    live_messages = [m for m in snapshot.get('messages', []) if m.get('turnId') == request.get('turn_id')]
    turns = [t for t in snapshot.get('turns', []) if t.get('id') == request.get('turn_id')]
    if (snapshot.get('history_truncated') or not sent_message(request, snapshot) or len(turns) != 1
            or live_messages != saved.get('messages')
            or turns[0].get('state') not in ('completed', 'failed')
            or {t['id'] for t in snapshot.get('turns', []) if t.get('state') != 'recovered'} - set(request['baseline']['turn_ids']) != {request.get('turn_id')}
            or turns[0].get('providerTurnId') != request.get('provider_turn_id')
            or snapshot.get('conversationId') != request['baseline']['conversation_id']
            or snapshot.get('activeBranchId') != request['baseline']['branch_id']):
        raise RoomError('Cannot attribute semantic outcome to an unchanged completed native result')
    # Later typed errors supplement the receipt; they never replace its bytes or usage.
    activities = activity_failures(snapshot, request['turn_id'])
    observed = {**saved, 'turn': turns[0], 'sessionFailures': snapshot.get('sessionFailures', []),
                'provider_failures': activities}
    native = None
    source = state.get('native_outcome_source')
    if source and source.get('session_id') == request['session_id']:
        from ao_native_outcome import inspect
        try:
            native = inspect(directory, state, request, source, snapshot)
        except (RoomError, OSError, ValueError, KeyError, TypeError) as exc:
            native = {'unknown': str(exc)[:500]}
        if native and native.get('next_human_uuid'):
            native = {**native, 'unknown': 'A later native human packet follows this owned request; reconcile it first'}
    value = {'version': 1, 'room_id': state['room_id'], 'request_id': request['request_id'],
             'receipt_sha256': request['receipt_sha256'], 'text_sha256': request['text_sha256'],
             'turn_id': request['turn_id'], 'provider_turn_id': request['provider_turn_id'],
             'ao_terminal': turns[0], 'session_failures': observed['sessionFailures'], 'provider_failures': activities,
             'native': native, 'outcome': classify(observed, native,
                require_structured=state['workflow'] == 'fable_engineering' or request['role'] == 'reviewer')}
    # A later quiet snapshot cannot clear an observed failure. Only the explicit
    # one-use continuation record can admit another request; original errors remain.
    old = load(directory, request) if request.get('semantic_outcome') else None
    if old and not value['outcome']['hold'] and (old['outcome']['kind'] in RESUMABLE
            or (old['outcome']['kind'] == 'unknown' and not allow_unknown_clear)):
        return old
    path = 'outcomes/' + request['request_id'] + '/' + digest(value) + '.json'
    if (directory / path).exists():
        if read(directory / path) != value:
            raise RoomError('Semantic outcome evidence was modified')
    else:
        atomic(directory / path, value)
    request.update(semantic_outcome=path, semantic_outcome_sha256=digest(value), semantic_status=value['outcome'])
    service.save(directory, state)
    return value


def load(directory, request):
    from ao_project_room import digest, read
    path = request.get('semantic_outcome')
    if not path:
        return None
    expected = 'outcomes/' + request['request_id'] + '/' + request.get('semantic_outcome_sha256', '') + '.json'
    if path != expected:
        raise RoomError('Semantic outcome has no exact owned path')
    record = read(directory / path)
    receipt(directory, request)
    if (digest(record) != request['semantic_outcome_sha256'] or record.get('version') != 1
            or record.get('room_id') != directory.name or record.get('request_id') != request['request_id']
            or record.get('receipt_sha256') != request['receipt_sha256']
            or record.get('text_sha256') != request['text_sha256']
            or record.get('outcome') != request.get('semantic_status')
            or record.get('turn_id') != request.get('turn_id')
            or record.get('provider_turn_id') != request.get('provider_turn_id')):
        raise RoomError('Semantic outcome evidence changed or belongs to another result')
    return record


def usable(directory, request):
    record = load(directory, request)
    value = record['outcome'] if record else classify(receipt(directory, request))
    if value['hold']:
        raise RoomError('Native semantic hold: ' + value['kind'] + '; ' + value['reason'])


def release_record(directory, state, request):
    from ao_project_room import digest, read
    pointer = request.get('outcome_resume')
    if not pointer:
        return None
    prefix = 'outcome-resumes/' + request['request_id']
    expected = prefix + '/' + request.get('outcome_resume_sha256', '') + '.json'
    if pointer not in (prefix + '.json', expected):
        raise RoomError('Outcome continuation has no exact owned path')
    value = read(directory / pointer)
    if (digest(value) != request.get('outcome_resume_sha256') or value.get('room_id') != state['room_id']
            or value.get('blocked_request_id') != request['request_id']):
        raise RoomError('Outcome continuation evidence was modified')
    return value


def gate(service, directory, state, role, new_request_id, snapshot):
    request = latest_for_role(state, role)
    if not request:
        return
    value = observe(service, directory, state, request, snapshot)
    if not value['outcome']['hold']:
        return
    release = release_record(directory, state, request)
    if (release and release['resume_request_id'] == new_request_id
            and release['outcome_sha256'] == request['semantic_outcome_sha256']):
        return
    raise RoomError('Native semantic hold: ' + value['outcome']['kind'] + '. Inspect ao_room_outcome_audit; no automatic retry or replay.')


def audit(service, directory, state, role='engineer', ao_database_path=None, native_transcript_path=None):
    request = latest_for_role(state, role)
    if not request:
        raise RoomError('Outcome audit requires a known completed owned request; uncertain delivery is not recoverable here')
    audit_quiet(service, directory, state, request)
    if bool(ao_database_path) != bool(native_transcript_path):
        raise RoomError('Supply both exact native evidence paths or neither')
    if ao_database_path:
        from ao_native_identity import read_owner
        from ao_native_outcome import validate_source
        owner = read_owner(ao_database_path, request['session_id'])
        source = {'database': ao_database_path, 'transcript': native_transcript_path,
                  'session_id': request['session_id'], 'native_session_id': owner['provider_conversation_id']}
        validate_source(state, source)
        # Validate the complete exact-turn source before persisting a path. An explicit
        # read-only audit can correct a moved source; prior outcome records retain it.
        from ao_native_outcome import inspect
        snapshot = service.identity(service.client(state), state, request)
        inspect(directory, state, request, source, snapshot)
        state['native_outcome_source'] = source
    snapshot = service.identity(service.client(state), state, request)
    value = observe(service, directory, state, request, snapshot, allow_unknown_clear=True)
    return {'request_id': request['request_id'], 'outcome': value['outcome'],
            'outcome_sha256': request['semantic_outcome_sha256'], 'native': value['native'],
            'resume_eligible': (value['outcome']['kind'] in RESUMABLE and not (value.get('native') or {}).get('unknown') if request['state'] == 'completed'
                                else native_quota_failure(value)),
            'model_dispatch': False, 'quota_reset_established': False}


def resume(service, directory, state, request_id, outcome_sha256, resume_request_id, diagnosis, authorization):
    from ao_project_room import atomic, digest, identifier, nonempty, read
    identifier(request_id); identifier(resume_request_id)
    nonempty(diagnosis, 'diagnosis', 6000); nonempty(authorization, 'authorization', 6000)
    request = state['requests'].get(request_id)
    if not request or latest_for_role(state, request['role']) != request:
        raise RoomError('Only the latest owned result can authorize an outcome continuation')
    audit_quiet(service, directory, state, request)
    inputs = {'version': 1, 'room_id': state['room_id'], 'blocked_request_id': request_id,
              'outcome_sha256': outcome_sha256, 'resume_request_id': resume_request_id,
              'diagnosis': diagnosis, 'authorization': authorization}
    prior = release_record(directory, state, request)
    if prior:
        if 'native_failure_settlement' in prior:
            inputs['native_failure_settlement'] = prior['native_failure_settlement']
            validate_settlement(directory, request)
        if {k: v for k, v in prior.items() if k != 'previous_resume_sha256'} == inputs:
            return {**prior, 'model_dispatch': False}
        if prior['resume_request_id'] != resume_request_id:
            raise RoomError('This outcome already has a different immutable continuation')
        inputs['previous_resume_sha256'] = request['outcome_resume_sha256']
    if request_id == resume_request_id or resume_request_id in state['requests']:
        raise RoomError('Continuation requires one unused request identity')
    snapshot = service.identity(service.client(state), state, request)
    value = observe(service, directory, state, request, snapshot)
    if (request['semantic_outcome_sha256'] != outcome_sha256 or value['outcome']['kind'] not in RESUMABLE
            or (value.get('native') or {}).get('unknown')):
        raise RoomError('Outcome evidence changed or does not establish an eligible diagnosed failure')
    if request['state'] != 'completed':
        if request['state'] not in ('uncertain', 'settled_failure') or not native_quota_failure(value):
            raise RoomError('Only an exactly correlated native quota failure can settle an uncertain AO failed turn')
        inputs['native_failure_settlement'] = {'prior_state': 'uncertain', 'ao_state': 'failed',
                                               'receipt_sha256': request['receipt_sha256']}
    path = 'outcome-resumes/' + request_id + '/' + digest(inputs) + '.json'
    if (directory / path).exists() and read(directory / path) != inputs:
        raise RoomError('A different continuation intent already exists')
    atomic(directory / path, inputs)
    request.update(outcome_resume=path, outcome_resume_sha256=digest(inputs))
    if 'native_failure_settlement' in inputs:
        request['state'] = 'settled_failure'  # Original AO receipt and failure remain immutable and unusable as success.
    service.save(directory, state)
    return {**inputs, 'model_dispatch': False}
