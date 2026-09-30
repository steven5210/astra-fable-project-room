"""Durable semantic holds, separate from AO transport and immutable receipts."""
import copy
import json
import re

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
    from ao_native_outcome import contradictory_stop
    if any(contradictory_stop(stop) for stop in stops):
        return {'kind': 'unknown', 'hold': True,
                'reason': 'A refusal or safety terminal stop contradicts provider-failure recovery; diagnose the refusal separately'}
    quota = any((isinstance(kind := (e.get('type') or e.get('kind') or e.get('error')), str) and kind in QUOTA_KINDS)
                or e.get('httpStatus', e.get('http_status')) == 429 for e in errors)
    if errors:
        return {'kind': 'quota_limit' if quota else 'provider_error', 'hold': True,
                'reason': 'Correlated native provider failure takes precedence over output/transport completion'}
    if native and native.get('unknown'):
        return {'kind': 'unknown', 'hold': True, 'reason': native['unknown']}
    if (native and not native.get('unknown') and native.get('expected_model')
            and isinstance(native.get('observed_models'), list) and not native['observed_models']):
        # A qualified family dispatch establishes its identity before inference; a turn with no
        # attributable native stop row is missing evidence and cannot become qualified success. A
        # typed native failure above still keeps its exact settlement lane.
        return {'kind': 'unknown', 'hold': True,
                'reason': 'Qualified native identity was not observed on the owned turn; missing evidence is not '
                          'qualified success'}
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
    return (record['outcome']['kind'] == 'quota_limit' and not inconclusive(record)
            and bool(native.get('anchor_uuid')) and native.get('next_human_uuid') is None
            and any(e.get('error') == 'rate_limit' or e.get('http_status') == 429 for e in native.get('errors', [])))


def inconclusive(record):
    return bool((record.get('native') or {}).get('unknown')
                or (record.get('observation_outcome') or {}).get('kind') == 'unknown')


def _disqualifying_token(value):
    # A bare typed ``limit`` is ambiguous here: the exact upstream vocabulary
    # is not established, so it conservatively disqualifies only the compaction
    # lane. Generic classification and the exact native quota contract remain
    # unchanged; this is not a quota assertion.
    return (isinstance(value, str)
            and (value in QUOTA_KINDS or value == 'limit' or 'quota' in value or 'rate_limit' in value
                 or 'refusal' in value or 'safety' in value))


def _contradictory_failure(value):
    """Check every typed field; a generic type must not shadow a specific category."""
    if not isinstance(value, dict):
        return True
    if value.get('httpStatus', value.get('http_status')) == 429:
        return True
    fields = []
    for key in ('type', 'kind', 'error', 'category', 'reason'):
        item = value.get(key)
        if isinstance(item, str):
            fields.append(item)
        elif isinstance(item, dict):
            fields.extend(item.get(nested) for nested in ('type', 'kind', 'error', 'category', 'reason')
                          if isinstance(item.get(nested), str))
    return any(_disqualifying_token(field) for field in fields)


def native_compaction_failure(record):
    '''Only the exact public autocompact-thrashing envelope, positively bound.'''
    from ao_native_outcome import COMPACTION_THRASHING_TEXT_SHA256, contradictory_stop
    native = record.get('native') or {}
    if record.get('outcome', {}).get('kind') != 'provider_error' or inconclusive(record):
        return False
    if native.get('unknown') or native.get('next_human_uuid') is not None:
        return False
    native_stops = native.get('stop_reasons')
    if not isinstance(native_stops, list) or any(contradictory_stop(stop) for stop in native_stops):
        return False
    proof = native.get('compaction_failure')
    keys = {'version', 'kind', 'error', 'error_role', 'error_model', 'error_stop_reason',
            'error_text_sha256', 'error_message_id', 'assistant_uuid', 'anchor_uuid',
            'next_human_uuid', 'later_substantive', 'http_status_absent', 'workspace_sha256'}
    if not isinstance(proof, dict) or set(proof) != keys:
        return False
    if (proof.get('version') != 1 or proof.get('kind') != 'autocompact_thrashing'
            or proof.get('error') != 'invalid_request' or proof.get('error_role') != 'assistant'
            or proof.get('error_model') != '<synthetic>' or proof.get('error_stop_reason') != 'stop_sequence'
            or proof.get('error_text_sha256') != COMPACTION_THRASHING_TEXT_SHA256
            or proof.get('next_human_uuid') is not None or proof.get('later_substantive') is not False
            or proof.get('http_status_absent') is not True
            or proof.get('anchor_uuid') != native.get('anchor_uuid')
            or not isinstance(proof.get('anchor_uuid'), str) or not proof['anchor_uuid']
            or not isinstance(proof.get('assistant_uuid'), str) or not proof['assistant_uuid']
            or proof['assistant_uuid'] == proof['anchor_uuid']
            or not (proof.get('error_message_id') is None
                    or (isinstance(proof.get('error_message_id'), str) and proof['error_message_id']))
            or not isinstance(proof.get('workspace_sha256'), str)
            or re.fullmatch(r'[0-9a-f]{64}', proof['workspace_sha256']) is None):
        return False
    errors = native.get('errors')
    if not isinstance(errors, list) or len(errors) != 1 or not isinstance(errors[0], dict):
        return False
    error = errors[0]
    if (error.get('uuid') != proof['assistant_uuid'] or error.get('error') != 'invalid_request'
            or error.get('http_status') is not None):
        return False
    settled = native.get('settled_errors') or []
    if not isinstance(settled, list):
        return False
    for projection in errors + settled:
        if (not isinstance(projection, dict) or projection.get('error') != 'invalid_request'
                or projection.get('http_status') is not None):
            return False
    terminal = record.get('ao_terminal')
    turn_id = record.get('turn_id')
    provider_turn_id = record.get('provider_turn_id')
    if isinstance(terminal, dict):
        turn_id = terminal.get('id', turn_id)
        provider_turn_id = terminal.get('providerTurnId', provider_turn_id)
        for key in ('error', 'failure'):
            value = terminal.get(key)
            if isinstance(value, str) and _disqualifying_token(value):
                return False
            if isinstance(value, dict) and _contradictory_failure(value):
                return False
        for key in ('stop_reason', 'stopReason'):
            if contradictory_stop(terminal.get(key)):
                return False
    failures = record.get('session_failures', [])
    activities = record.get('provider_failures', [])
    if not isinstance(failures, list) or not isinstance(activities, list):
        return False
    for failure in failures:
        if not isinstance(failure, dict):
            return False
        if failure.get('turnId') is None and failure.get('providerTurnId') is None:
            return False
        if (failure.get('turnId') == turn_id
                or (provider_turn_id and failure.get('providerTurnId') == provider_turn_id)):
            if _contradictory_failure(failure):
                return False
    for activity in activities:
        if not isinstance(activity, dict) or _contradictory_failure(activity):
            return False
    return True


def native_failure_kind(record):
    if inconclusive(record):
        return None
    if native_quota_failure(record):
        return 'quota_limit'
    if native_compaction_failure(record):
        return 'compaction_thrashing'
    return None


RESTORED_COMPACTION_KIND = 'restored_compaction_message'
RESTORED_COMPACTION_FIELDS = frozenset((
    'version', 'kind', 'room_id', 'request_id', 'session_id', 'conversation_id', 'branch_id', 'turn_id',
    'provider_turn_id', 'receipt_sha256', 'prior_outcome_sha256', 'native_source', 'native_source_sha256',
    'native_compaction_failure', 'added_message', 'added_message_sha256', 'live_messages_sha256'))
RESTORED_MESSAGE_ID = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,100}')
RESTORED_CONTRADICTORY_KEYS = ('error', 'errorMessage', 'failure', 'apiError', 'apiErrorStatus',
                               'rateLimitType', 'quota', 'quotaLimits', 'refusal', 'safety',
                               'stopReason', 'stop_reason', 'httpStatus', 'http_status')


def _restored_metadata_absent(value):
    """Whether one restored-row contradictory-metadata key carries exact legitimate absence.

    Only ``None``, ``False``, ``''``, ``[]`` and ``{}`` are absent. Identity and exact type
    checks keep ``False`` admissible deliberately while rejecting falsy numerics: ``0`` and
    ``0.0`` compare equal to ``False`` in Python, but they are malformed metadata, not absence.
    This is a pure predicate; it never authorizes anything.
    """
    if value is None or value is False:
        return True
    if isinstance(value, str):
        return value == ''
    if isinstance(value, (list, dict)):
        return not value
    return False


def _restored_compaction_extra(saved, live_messages):
    """The exact single AO restoration row of the retained synthetic thrashing error, or None.

    Pure structure only: the unchanged saved turn messages must be the exact prefix and exactly one
    provider-origin, nonstreaming assistant row with the exact public error text, one well-formed
    unique message identity and no contradictory metadata may follow. This never authorizes anything.
    """
    from ao_native_outcome import COMPACTION_THRASHING_TEXT
    if not isinstance(saved, dict) or not isinstance(live_messages, list) or not live_messages:
        return None
    messages = saved.get('messages')
    if (not isinstance(messages, list) or not messages
            or any(not isinstance(item, dict) for item in messages + live_messages)
            or len(live_messages) != len(messages) + 1):
        return None
    if live_messages[:len(messages)] != messages:
        return None
    extra = live_messages[-1]
    if (extra.get('role') != 'assistant' or extra.get('origin') != 'provider'
            or extra.get('streaming') is not False):
        return None
    if extra.get('text') != COMPACTION_THRASHING_TEXT:
        return None
    if 'kind' in extra and extra.get('kind') != 'message':
        return None
    if any(not _restored_metadata_absent(extra.get(key)) for key in RESTORED_CONTRADICTORY_KEYS):
        return None
    identity = extra.get('id')
    if not isinstance(identity, str) or RESTORED_MESSAGE_ID.fullmatch(identity) is None:
        return None
    identities = [message.get('id') for message in live_messages]
    if any(not isinstance(value, str) or not value for value in identities):
        return None
    if len(set(identities)) != len(identities) or identities.count(identity) != 1:
        return None
    return extra


def _restored_compaction_candidate(request, saved, live_messages, turn):
    """Cheap eligibility preconditions before the native inspection; this never authorizes."""
    if (not isinstance(request, dict) or request.get('role') != 'engineer'
            or request.get('state') not in ('uncertain', 'settled_failure')
            or saved.get('history_truncated') is not False
            or not isinstance(request.get('turn_id'), str) or not request['turn_id']
            or not isinstance(request.get('provider_turn_id'), str) or not request['provider_turn_id']
            or not isinstance(turn, dict) or turn.get('state') != 'failed'
            or turn.get('providerTurnId') != request.get('provider_turn_id')):
        return None
    return _restored_compaction_extra(saved, live_messages)


def _outcome_record_by_sha(directory, request, sha):
    """The exact owned content-addressed outcome record for one SHA, or None. Read-only."""
    from ao_project_room import digest, read
    if (not isinstance(request, dict) or not isinstance(sha, str)
            or re.fullmatch(r'[0-9a-f]{64}', sha) is None):
        return None
    try:
        record = read(directory / ('outcomes/' + str(request.get('request_id')) + '/' + sha + '.json'))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if (not isinstance(record, dict) or digest(record) != sha
            or record.get('version') != 1 or record.get('room_id') != directory.name
            or record.get('request_id') != request.get('request_id')
            or record.get('receipt_sha256') != request.get('receipt_sha256')
            or record.get('text_sha256') != request.get('text_sha256')
            or record.get('turn_id') != request.get('turn_id')
            or record.get('provider_turn_id') != request.get('provider_turn_id')):
        return None
    return record


def _owned_outcome_record(directory, request):
    """The record this request currently names at its exact owned path, or None. Read-only."""
    path = request.get('semantic_outcome') if isinstance(request, dict) else None
    sha = request.get('semantic_outcome_sha256') if isinstance(request, dict) else None
    if (not isinstance(path, str) or not isinstance(sha, str)
            or path != 'outcomes/' + str(request.get('request_id')) + '/' + sha + '.json'):
        return None
    return _outcome_record_by_sha(directory, request, sha)


def _verified_restored_observation(directory, request, record, saved, live_messages, native=None,
                                   require_fresh_native=False):
    """Verify one retained restored-compaction observation against the exact current AO rows.

    Read-only. ``native``, when supplied, is the fresh native inspection that must still positively
    reproduce the bound compaction failure. ``require_fresh_native`` makes that fresh proof mandatory
    for a current restoration recheck: an absent native inspection is not evidence of restoration and
    must refuse before any write. A missing, corrupt, stale or contradictory observation returns None;
    the caller refuses and never modifies existing evidence.
    """
    from ao_project_room import digest
    observation = record.get('restored_compaction_observation') if isinstance(record, dict) else None
    if (not isinstance(observation, dict) or set(observation) != RESTORED_COMPACTION_FIELDS
            or observation.get('version') != 1 or observation.get('kind') != RESTORED_COMPACTION_KIND):
        return None
    if (request.get('role') != 'engineer' or request.get('state') not in ('uncertain', 'settled_failure')
            or not isinstance(request.get('provider_turn_id'), str) or not request['provider_turn_id']
            or not isinstance(request.get('baseline'), dict)):
        return None
    baseline = request['baseline']
    if (observation.get('room_id') != directory.name
            or observation.get('request_id') != request.get('request_id')
            or observation.get('session_id') != request.get('session_id')
            or observation.get('conversation_id') != baseline.get('conversation_id')
            or observation.get('branch_id') != baseline.get('branch_id')
            or observation.get('turn_id') != request.get('turn_id')
            or observation.get('provider_turn_id') != request.get('provider_turn_id')
            or observation.get('receipt_sha256') != request.get('receipt_sha256')
            or not isinstance(observation.get('native_source'), dict)
            or not isinstance(observation.get('native_source_sha256'), str)
            or re.fullmatch(r'[0-9a-f]{64}', observation['native_source_sha256']) is None
            or not isinstance(observation.get('native_compaction_failure'), dict)):
        return None
    extra = _restored_compaction_extra(saved, live_messages)
    if extra is None or observation.get('added_message') != extra:
        return None
    if (observation.get('added_message_sha256') != digest(extra)
            or observation.get('live_messages_sha256') != digest(live_messages)):
        return None
    retained = record.get('native') if isinstance(record.get('native'), dict) else {}
    if (not isinstance(record.get('outcome'), dict) or record['outcome'].get('kind') != 'provider_error'
            or observation.get('native_source') != retained.get('source')
            or observation.get('native_source_sha256') != retained.get('source_sha256')
            or observation.get('native_compaction_failure') != retained.get('compaction_failure')
            or not native_compaction_failure(record)):
        return None
    prior = _outcome_record_by_sha(directory, request, observation.get('prior_outcome_sha256'))
    if (prior is None or 'restored_compaction_observation' in prior
            or not isinstance(prior.get('outcome'), dict)
            or native_failure_kind(prior) != 'compaction_thrashing'):
        return None
    prior_native = prior.get('native') if isinstance(prior.get('native'), dict) else {}
    if (prior_native.get('source') != observation.get('native_source')
            or prior_native.get('source_sha256') != observation.get('native_source_sha256')
            or prior_native.get('compaction_failure') != observation.get('native_compaction_failure')):
        return None
    if require_fresh_native and not isinstance(native, dict):
        return None
    if native is not None:
        if (not isinstance(native, dict) or native.get('unknown')
                or native.get('next_human_uuid') is not None
                or native.get('source') != observation.get('native_source')
                or native.get('source_sha256') != observation.get('native_source_sha256')
                or native.get('compaction_failure') != observation.get('native_compaction_failure')
                or native_failure_kind({**record, 'native': native}) != 'compaction_thrashing'):
            return None
    return observation


def _retained_restored_compaction_record(directory, request):
    """The selected current outcome record that retains this request's restored-compaction observation.

    Read-only. Only the request's own selected current outcome pointer is returned, and only when its
    record is coherent, valid and non-foreign and itself retains the observation. The authenticated
    release-named settlement record is read only to detect that a settled failed turn carries this
    restoration obligation; it never replaces missing, malformed, foreign or lost selected evidence.
    A detected obligation whose selected current outcome is unusable, or names other evidence, fails
    closed here. A request with no restoration obligation returns None and keeps its own strict
    comparison.
    """
    record = _owned_outcome_record(directory, request)
    if isinstance(record, dict) and 'restored_compaction_observation' in record:
        return record
    obligation = False
    if isinstance(request, dict) and request.get('state') == 'settled_failure':
        release = release_record(directory, {'room_id': directory.name}, request)
        if isinstance(release, dict) and isinstance(release.get('outcome_sha256'), str):
            retained = _outcome_record_by_sha(directory, request, release['outcome_sha256'])
            obligation = isinstance(retained, dict) and 'restored_compaction_observation' in retained
    if obligation:
        # The release proves this consumed turn was settled under a retained restored-compaction
        # observation, so the selected current outcome must itself be exactly that evidence.
        raise RoomError('The retained restored compaction-error observation no longer matches the exact AO evidence; re-audit before continuing')
    return None


def _verified_restored_compaction_rows(directory, request, saved, live_messages):
    """The verified restored-compaction observation for the exact current rows, or None.

    None means this request retains no restored-compaction observation and the caller keeps its own
    strict comparison. A retained observation that no longer matches the exact current rows raises:
    a stale proof refuses and is never re-minted by a read-only consumer.
    """
    record = _retained_restored_compaction_record(directory, request)
    if not isinstance(record, dict):
        return None
    observation = _verified_restored_observation(directory, request, record, saved, live_messages)
    if observation is None:
        raise RoomError('The retained restored compaction-error observation no longer matches the exact AO evidence; re-audit before continuing')
    return observation


def _authorize_restored_compaction(directory, request, saved, live_messages, extra, observed, native, prior):
    """Create the immutable restored-compaction observation. Explicit outcome audit only."""
    from ao_project_room import digest
    turn = observed.get('turn') if isinstance(observed, dict) else None
    if (request.get('role') != 'engineer' or request.get('state') not in ('uncertain', 'settled_failure')
            or not isinstance(request.get('turn_id'), str) or not request['turn_id']
            or not isinstance(request.get('provider_turn_id'), str) or not request['provider_turn_id']
            or saved.get('history_truncated') is not False
            or not isinstance(saved.get('turn'), dict) or saved['turn'].get('state') != 'failed'
            or not isinstance(turn, dict) or turn.get('state') != 'failed'
            or turn.get('providerTurnId') != request.get('provider_turn_id')
            or extra is None or _restored_compaction_extra(saved, live_messages) != extra):
        raise RoomError('A restored compaction-error observation requires one exact failed engineer turn and its unchanged saved failure')
    if (not isinstance(prior, dict) or 'restored_compaction_observation' in prior
            or not isinstance(prior.get('outcome'), dict)
            or native_failure_kind(prior) != 'compaction_thrashing'):
        raise RoomError('A restored compaction-error observation requires the prior retained diagnosed native compaction outcome; audit the unchanged native failure first')
    source = native.get('source') if isinstance(native, dict) else None
    if (not isinstance(native, dict) or native.get('unknown') or native.get('next_human_uuid') is not None
            or not isinstance(source, dict)
            or set(source) != {'database', 'transcript', 'session_id', 'native_session_id'}
            or any(not isinstance(value, str) or not value for value in source.values())
            or source.get('session_id') != request.get('session_id')
            or not isinstance(native.get('source_sha256'), str)
            or re.fullmatch(r'[0-9a-f]{64}', native['source_sha256']) is None
            or not isinstance(native.get('compaction_failure'), dict)
            or native_failure_kind({'outcome': prior['outcome'], 'native': native, 'ao_terminal': turn,
                                    'session_failures': observed.get('sessionFailures'),
                                    'provider_failures': observed.get('provider_failures')}) != 'compaction_thrashing'):
        raise RoomError('A restored compaction-error observation requires the same positively typed native compaction failure from a fresh owner/source inspection')
    retained = prior.get('native') if isinstance(prior.get('native'), dict) else {}
    if (source != retained.get('source')
            or native.get('source_sha256') != retained.get('source_sha256')
            or native.get('compaction_failure') != retained.get('compaction_failure')
            or native.get('anchor_uuid') != retained.get('anchor_uuid')
            or native.get('errors') != retained.get('errors')):
        raise RoomError('A restored compaction-error observation requires the exact native error identity, source paths and complete transcript hash already retained')
    return {'version': 1, 'kind': RESTORED_COMPACTION_KIND, 'room_id': directory.name,
            'request_id': request.get('request_id'), 'session_id': request.get('session_id'),
            'conversation_id': (request.get('baseline') or {}).get('conversation_id'),
            'branch_id': (request.get('baseline') or {}).get('branch_id'),
            'turn_id': request.get('turn_id'), 'provider_turn_id': request.get('provider_turn_id'),
            'receipt_sha256': request.get('receipt_sha256'), 'prior_outcome_sha256': digest(prior),
            'native_source': copy.deepcopy(source), 'native_source_sha256': native['source_sha256'],
            'native_compaction_failure': copy.deepcopy(native['compaction_failure']),
            'added_message': copy.deepcopy(extra), 'added_message_sha256': digest(extra),
            'live_messages_sha256': digest(live_messages)}


def validate_settlement(directory, request):
    from ao_project_room import digest, read
    release = release_record(directory, {'room_id': directory.name}, request)
    if not release:
        raise RoomError('Settled native failure lacks its audited continuation')
    proof = release.get('native_failure_settlement') or {}
    evidence_path = 'outcomes/' + request['request_id'] + '/' + release.get('outcome_sha256', '') + '.json'
    record = read(directory / evidence_path)
    base = {'prior_state': 'uncertain', 'ao_state': 'failed', 'receipt_sha256': request['receipt_sha256']}
    native = record.get('native') or {}
    source = native.get('source')
    source_ok = (isinstance(source, dict)
                 and set(source) == {'database', 'transcript', 'session_id', 'native_session_id'}
                 and all(isinstance(value, str) and value for value in source.values())
                 and source.get('session_id') == request.get('session_id')
                 and isinstance(native.get('source_sha256'), str)
                 and re.fullmatch(r'[0-9a-f]{64}', native['source_sha256']) is not None)
    quota = isinstance(proof, dict) and proof == base and native_quota_failure(record)
    compaction = (
        request.get('role') == 'engineer'
        and isinstance(proof, dict)
        and set(proof) == set(base) | {'kind', 'required_guard_sha256'}
        and proof.get('kind') == 'compaction_thrashing'
        and isinstance(proof.get('required_guard_sha256'), str)
        and re.fullmatch(r'[0-9a-f]{64}', proof['required_guard_sha256']) is not None
        and all(proof.get(key) == value for key, value in base.items())
        and source_ok and native_compaction_failure(record)
    )
    if (digest(release) != request.get('outcome_resume_sha256')
            or release.get('room_id') != directory.name or release.get('blocked_request_id') != request['request_id']
            or digest(record) != release.get('outcome_sha256') or not (quota or compaction)
            or record.get('receipt_sha256') != request['receipt_sha256']
            or record.get('turn_id') != request.get('turn_id')
            or record.get('provider_turn_id') != request.get('provider_turn_id')
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


def observe(service, directory, state, request, snapshot, allow_unknown_clear=False, source_verification=None,
            restored_compaction=False):
    try:
        return _observe(service, directory, state, request, snapshot, allow_unknown_clear, source_verification,
                        restored_compaction)
    except (RoomError, OSError, ValueError, KeyError, TypeError) as exc:
        from ao_history_reconciliation import invalidate
        if request.get('history_reconciliation_sha256'):
            invalidate(service, directory, state, request['request_id'], exc)
        raise


def _observe(service, directory, state, request, snapshot, allow_unknown_clear=False, source_verification=None,
             restored_compaction=False):
    from ao_project_room import atomic, digest, read, sent_message, turn_ids
    from ao_project_room import native_turn_identity
    identity = native_turn_identity(request)
    if identity and not request.get('provider_turn_id'):
        request['provider_turn_id'] = identity
    saved = receipt(directory, request)
    live_messages = [m for m in snapshot.get('messages', []) if m.get('turnId') == request.get('turn_id')]
    turns = [t for t in snapshot.get('turns', []) if t.get('id') == request.get('turn_id')]
    # The exact single-message AO restoration of the retained synthetic thrashing error is only
    # structurally admissible here so an explicit audit can authorize it or a retained authorization
    # can be verified below; this guard alone never authorizes the exception and every other strict
    # comparison is unchanged.
    restored_candidate = None
    if live_messages != saved.get('messages') and len(turns) == 1:
        restored_candidate = _restored_compaction_candidate(request, saved, live_messages, turns[0])
    if (snapshot.get('history_truncated') or not sent_message(request, snapshot) or len(turns) != 1
            or (live_messages != saved.get('messages') and restored_candidate is None)
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
    from ao_native_outcome import source_keys
    source_key, source_evidence_key = source_keys(request['role'])
    source = state.get(source_key)
    if source and source.get('session_id') == request['session_id']:
        from ao_native_outcome import inspect
        try:
            native = inspect(directory, state, request, source, snapshot)
        except (RoomError, OSError, ValueError, KeyError, TypeError) as exc:
            native = {'unknown': str(exc)[:500]}
        if native and native.get('next_human_uuid'):
            native = {**native, 'unknown': 'A later native human packet follows this owned request; reconcile it first'}
    restored_observation = None
    prior_record = _owned_outcome_record(directory, request)
    # The authenticated release names this request's own retained restoration obligation even when
    # its selected current outcome pointer is missing, malformed, foreign, names other evidence, or
    # the live rows no longer differ from the saved receipt. Enforce it here, in the shared
    # observation path before any outcome, state or pointer write, so lost or foreign selected
    # current proof fails closed instead of minting an unannotated replacement outcome. The release
    # only detects the obligation; it never substitutes for missing, malformed or foreign selected
    # evidence. A legitimate pre-restoration record still supplies the prior evidence for the
    # explicit-audit-only authorization below, and an unrelated outcome keeps its existing path.
    retained_record = _retained_restored_compaction_record(directory, request)
    if isinstance(retained_record, dict):
        prior_record = retained_record
    restoration_retained = isinstance(retained_record, dict)
    if live_messages != saved.get('messages') or restoration_retained:
        # The narrow restoration contract positively requires known complete history. The ordinary
        # legacy path below keeps its existing truthy check for unrelated outcomes.
        if snapshot.get('history_truncated') is not False:
            raise RoomError('Restored compaction-error evidence requires positively complete native history')
    if live_messages != saved.get('messages'):
        if restoration_retained:
            restored_observation = _verified_restored_observation(directory, request, prior_record, saved,
                                                                  live_messages, native=native,
                                                                  require_fresh_native=True)
            if restored_observation is None:
                raise RoomError('The retained restored compaction-error observation no longer matches the exact AO evidence; re-audit before continuing')
        elif restored_compaction:
            restored_observation = _authorize_restored_compaction(directory, request, saved, live_messages,
                                                                  restored_candidate, observed, native, prior_record)
        else:
            raise RoomError('Cannot attribute semantic outcome to an unchanged completed native result')
    elif restoration_retained:
        raise RoomError('The retained restored compaction-error observation no longer matches the exact AO evidence; re-audit before continuing')
    extra_turns = turn_ids(snapshot) - set(request['baseline']['turn_ids']) - {request['turn_id']}
    if state['workflow'] == 'fable_engineering':
        proven_imports = _unowned_context_turns(state, {
            p['turn_id'] for field in ('compaction_imports', 'task_notification_imports')
            for p in (native or {}).get(field, [])}) if not (native or {}).get('unknown') else set()
        if extra_turns - proven_imports:
            raise RoomError('Native history contains unverified recovered context; audit its exact source before continuing')
    from ao_history_reconciliation import reconcile
    proof_sha256 = reconcile(service, directory, state, request, saved, observed, snapshot, native, allow_unknown_clear)
    if proof_sha256:
        observed = {**observed, 'history_truncated': False}
    value = {'version': 1, 'room_id': state['room_id'], 'request_id': request['request_id'],
             'receipt_sha256': request['receipt_sha256'], 'text_sha256': request['text_sha256'],
             'turn_id': request['turn_id'], 'provider_turn_id': request['provider_turn_id'],
             'ao_terminal': turns[0], 'session_failures': observed['sessionFailures'], 'provider_failures': activities,
             'native': native, 'outcome': classify(observed, native,
                require_structured=state['workflow'] == 'fable_engineering' or request['role'] == 'reviewer')}
    if restored_observation is not None:
        value['restored_compaction_observation'] = restored_observation
    # W3: this request's own frozen worker expectation is a separate proof. The reviewed W2 consumer
    # runs before any legacy absent-field path, a worker identity gap is recorded as its own
    # structured evidence, and only qualified final availability is withheld: the parent native
    # outcome, its provider/quota lanes, refusals, truncation and original digests are untouched.
    from ao_worker_identity import gated_outcome, observe_workers
    worker_evidence = observe_workers(directory, state, request, saved, native)
    if worker_evidence is not None:
        value['worker_observations'] = worker_evidence
        value['outcome'] = gated_outcome(value['outcome'], worker_evidence)
    if proof_sha256:
        value['history_reconciliation_sha256'] = proof_sha256
    old = load(directory, request) if request.get('semantic_outcome') else None
    prior_source_verification = (old or {}).get('native_source_verification')
    if source_verification is not None:
        value['native_source_verification'] = source_verification
    elif isinstance(prior_source_verification, dict) and prior_source_verification.get('source') == source:
        # Keep the prior explicit audit's proof. Merely observing the same result
        # must not drop metadata and invalidate its exact continuation digest.
        value['native_source_verification'] = prior_source_verification
    if old and old.get('resolved_observation_sha256'):
        value['resolved_observation_sha256'] = old['resolved_observation_sha256']
    if (old and old['outcome']['kind'] in RESUMABLE and inconclusive(old)
            and (allow_unknown_clear or source_verification is not None)
            and value['outcome']['kind'] != 'unknown' and not inconclusive(value)):
        # An explicit resolution must not recreate an earlier outcome hash and
        # reactivate its old continuation authority. Retain the diagnosed record.
        value['resolved_observation_sha256'] = digest(old)
    # A later quiet snapshot cannot clear an observed failure. Only the explicit
    # one-use continuation record can admit another request; original errors remain.
    if old and old['outcome']['kind'] in RESUMABLE and value['outcome']['kind'] == 'unknown':
        # Preserve the known failure and the newer uncertainty independently. Its
        # changed digest invalidates prior continuation authority; uncertainty is
        # never converted into a diagnosed failure merely by retaining its kind.
        value['observation_outcome'] = value['outcome']
        value['outcome'] = old['outcome']
    elif (old and old['outcome']['kind'] in RESUMABLE and inconclusive(old)
            and not allow_unknown_clear and source_verification is None):
        value['observation_outcome'] = old.get('observation_outcome') or {
            'kind': 'unknown', 'hold': True, 'reason': old['native']['unknown']}
        value['outcome'] = old['outcome']
    elif old and not value['outcome']['hold'] and (old['outcome']['kind'] in RESUMABLE
            or (old['outcome']['kind'] == 'unknown' and (not allow_unknown_clear or source_verification is not None))):
        if source_verification is None and not (allow_unknown_clear and inconclusive(old)):
            return old
        value['outcome'] = old['outcome']  # A verified source move cannot clear the existing sticky hold.
    path = 'outcomes/' + request['request_id'] + '/' + digest(value) + '.json'
    if (directory / path).exists():
        if read(directory / path) != value:
            raise RoomError('Semantic outcome evidence was modified')
    else:
        atomic(directory / path, value)
    request.update(semantic_outcome=path, semantic_outcome_sha256=digest(value), semantic_status=value['outcome'])
    if source_verification is not None:
        state[source_evidence_key] = {'path': path, 'sha256': digest(value), 'request_id': request['request_id']}
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
    if record.get('history_reconciliation_sha256'):
        from ao_history_reconciliation import load_proof, PROOF
        load_proof(directory, {**request, PROOF: record[PROOF]})
    return record


def _worker_boundary_notice(request):
    carried = request.get("carried") if isinstance(request, dict) else None
    notices = carried.get("boundary_notices") if isinstance(carried, dict) else None
    if not isinstance(notices, list):
        return False
    from ao_model_boundaries import WORKER_ROUTING
    return any(isinstance(notice, dict) and notice.get("kind") == WORKER_ROUTING for notice in notices)


def _requires_worker_proof(request, record):
    """Whether final availability must rest on qualified W3 worker evidence.

    A present frozen field always requires it. A retained worker observation is honored even when
    the request field was deleted, so a newly created worker hold cannot be discarded merely by
    deleting the field. A delegation-capable request that carries this room's committed worker
    boundary notice is a routing-era request and also requires the proof. Genuine pre-routing
    history has no field, no retained observation and no worker notice, so it keeps its old path.
    """
    if not isinstance(request, dict) or request.get("role") != "engineer":
        return bool(isinstance(record, dict) and "worker_observations" in record)
    delegating = request.get("purpose") in ("implementation", "correction")
    if "native_worker_expectations" in request:
        return True
    if isinstance(record, dict) and "worker_observations" in record:
        return True
    return delegating and _worker_boundary_notice(request)


def usable(directory, request):
    record = load(directory, request)
    # W3: a present frozen worker expectation, or a request whose retained outcome already records
    # a worker observation, is part of final availability even when the request field was deleted.
    # A newly created worker hold cannot be accepted merely because that field is absent. Before any
    # retained worker evidence is trusted, this request's own persisted receipt and its conditional
    # projection binding are authenticated through the reviewed W2 receipt-only primitive, using the
    # retained in-memory request directly: no room state is reloaded, no second native source scan
    # runs, and the historical-epoch expectation read stays in the guarded observer. Deleting the
    # frozen expectation or the prompt projection -- including both on a previously qualified record
    # whose receipt is unchanged -- is a provenance failure, never usable history.
    if _requires_worker_proof(request, record):
        from ao_model_boundaries import _authenticated_request
        _authenticated_request(directory, request)
        evidence = (record or {}).get('worker_observations')
        if not (isinstance(evidence, dict) and evidence.get('qualified')):
            raise RoomError('Native semantic hold: qualified native worker identity is not established '
                            'for this request; re-observe the owned result and inspect its retained worker evidence')
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


def _reviewed_guard_sha256():
    from ao_project_room import digest
    from pathlib import Path
    try:
        from ao_routing_guard import READ_ADMISSION_VERSION
    except ImportError as exc:
        raise RoomError('The reviewed read-admission guard is not installed; compaction failure cannot be settled') from exc
    if READ_ADMISSION_VERSION != 1:
        raise RoomError('The installed read-admission guard is not review version 1; compaction failure cannot be settled')
    path = Path(__file__).with_name('ao_routing_guard.py')
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise RoomError('The reviewed read-admission guard source is unavailable; compaction failure cannot be settled') from exc
    return digest(raw)


def _require_compaction_mitigation(service, directory, state, request, release, diagnosed=False):
    proof = release.get('native_failure_settlement') or {}
    if proof.get('kind') != 'compaction_thrashing':
        if diagnosed:
            raise RoomError('Autocompact-thrashing evidence requires an exact native compaction settlement; this continuation carries no mitigation')
        return
    required = proof.get('required_guard_sha256')
    if not isinstance(required, str) or re.fullmatch(r'[0-9a-f]{64}', required) is None:
        raise RoomError('Compaction-thrashing continuation lacks its exact reviewed guard identity')
    try:
        from ao_delegates import validate_preparation
        from ao_routing import validate_local
        from ao_routing_refresh import effective
        prepared = validate_preparation(directory, state, request['session_id'])
        validate_local(prepared, state, directory)
        routing = effective(directory, state, prepared) if state.get('routing_refresh') else prepared.get('routing')
    except (RoomError, OSError, ValueError, KeyError, TypeError, ImportError) as exc:
        raise RoomError('Compaction-thrashing continuation requires the reviewed read-admission routing guard; perform the supported stopped-controller refresh before sending') from exc
    if not isinstance(routing, dict) or routing.get('guard_sha256') != required:
        raise RoomError('Compaction-thrashing continuation requires the reviewed read-admission routing guard; the effective guard does not match the settled mitigation')


def _fresh_failed_turn_evidence(directory, request, snapshot, record):
    """Read-only reconstruction of the exact current AO evidence a fresh audit uses.

    A changed current receipt, provider turn, activity or session-failure record
    must refuse an unused compaction refresh before it can carry a stale proof
    forward. When the retained record carries an authorized restored-compaction
    observation, the current rows must still be exactly the ones that record
    observed; this function verifies that authority and never creates one. It
    never writes an outcome, saves state or dispatches a model.
    """
    from ao_project_room import sent_message
    saved = receipt(directory, request)
    turns = [t for t in snapshot.get('turns', []) if t.get('id') == request.get('turn_id')]
    messages = [m for m in snapshot.get('messages', []) if m.get('turnId') == request.get('turn_id')]
    baseline = request.get('baseline') or {}
    if (snapshot.get('history_truncated') is not False or not sent_message(request, snapshot) or len(turns) != 1
            or not isinstance(turns[0], dict)
            or turns[0].get('state') != 'failed'
            or turns[0].get('providerTurnId') != request.get('provider_turn_id')
            or snapshot.get('conversationId') != baseline.get('conversation_id')
            or snapshot.get('activeBranchId') != baseline.get('branch_id')):
        raise RoomError('Unused compaction continuation AO evidence changed; re-audit before continuing')
    if isinstance(record, dict) and record.get('restored_compaction_observation') is not None:
        if _verified_restored_observation(directory, request, record, saved, messages) is None:
            raise RoomError('Unused compaction continuation AO evidence changed; re-audit before continuing')
    elif messages != saved.get('messages'):
        raise RoomError('Unused compaction continuation AO evidence changed; re-audit before continuing')
    return {**saved, 'turn': turns[0], 'sessionFailures': snapshot.get('sessionFailures', []),
            'provider_failures': activity_failures(snapshot, request['turn_id'])}


def verify_unused_compaction_source(directory, state, request, snapshot):
    """Read-only fresh verification of an unused compaction continuation source.

    A settlement that already has a legitimate successor keeps its immutable
    historical validation. Only the currently unused successor must still
    reproduce the exact saved native proof and source digest at a refresh
    boundary. This function never writes or mutates state; stale evidence
    refuses before any refresh intent or runtime replacement.
    """
    from ao_native_outcome import inspect
    from ao_project_room import digest, read
    release = release_record(directory, state, request)
    proof = (release or {}).get('native_failure_settlement') or {}
    if proof.get('kind') != 'compaction_thrashing':
        return
    if release.get('resume_request_id') in state.get('requests', {}):
        return
    path = request.get('semantic_outcome')
    expected = 'outcomes/' + request['request_id'] + '/' + request.get('semantic_outcome_sha256', '') + '.json'
    if not path or path != expected:
        raise RoomError('Unused compaction continuation has no exact owned outcome record')
    record = read(directory / path)
    if (digest(record) != request.get('semantic_outcome_sha256')
            or record.get('request_id') != request['request_id']
            or record.get('receipt_sha256') != request.get('receipt_sha256')
            or record.get('turn_id') != request.get('turn_id')
            or record.get('provider_turn_id') != request.get('provider_turn_id')):
        raise RoomError('Unused compaction continuation outcome evidence changed; re-audit before continuing')
    native = record.get('native') or {}
    source = native.get('source')
    if (not isinstance(source, dict) or native.get('unknown')
            or not isinstance(native.get('source_sha256'), str)
            or not isinstance(native.get('compaction_failure'), dict)):
        raise RoomError('Unused compaction continuation lacks its exact native source evidence')
    try:
        current = inspect(directory, state, request, source, snapshot)
    except (RoomError, OSError, ValueError, KeyError, TypeError) as exc:
        raise RoomError('Unused compaction continuation source cannot be verified; re-audit before continuing') from exc
    if (current.get('unknown') or current.get('next_human_uuid') is not None
            or current.get('source_sha256') != native['source_sha256']
            or current.get('compaction_failure') != native['compaction_failure']):
        raise RoomError('Unused compaction continuation source changed; re-audit before continuing')
    observed = _fresh_failed_turn_evidence(directory, request, snapshot, record)
    fresh = {**record, 'ao_terminal': observed['turn'], 'session_failures': observed['sessionFailures'],
             'provider_failures': observed['provider_failures'], 'native': current,
             'outcome': classify(observed, current, require_structured=(
                 state.get('workflow') == 'fable_engineering' or request.get('role') == 'reviewer'))}
    if (release.get('outcome_sha256') != request.get('semantic_outcome_sha256')
            or inconclusive(fresh) or native_failure_kind(fresh) != 'compaction_thrashing'
            or digest(fresh) != release.get('outcome_sha256')):
        raise RoomError('Unused compaction continuation AO evidence changed; re-audit before continuing')


def gate(service, directory, state, role, new_request_id, snapshot):
    request = latest_for_role(state, role)
    if not request:
        return
    value = observe(service, directory, state, request, snapshot)
    if not value['outcome']['hold']:
        return
    release = release_record(directory, state, request)
    if (release and release['resume_request_id'] == new_request_id
            and release['outcome_sha256'] == request['semantic_outcome_sha256'] and not inconclusive(value)):
        diagnosed = native_failure_kind(value) == 'compaction_thrashing'
        if diagnosed and request.get('state') == 'completed':
            raise RoomError('AO-completed autocompact-thrashing evidence is not eligible for recovery; only an exact failed AO transport turn can settle this native failure')
        _require_compaction_mitigation(service, directory, state, request, release, diagnosed)
        return
    raise RoomError('Native semantic hold: ' + value['outcome']['kind'] + '. Inspect ao_room_outcome_audit; no automatic retry or replay.')


def audit(service, directory, state, role='engineer', ao_database_path=None, native_transcript_path=None):
    request = latest_for_role(state, role)
    if not request:
        raise RoomError('Outcome audit requires a known completed owned request; uncertain delivery is not recoverable here')
    audit_quiet(service, directory, state, request)
    if bool(ao_database_path) != bool(native_transcript_path):
        raise RoomError('Supply both exact native evidence paths or neither')
    source_verification = None
    if ao_database_path:
        from ao_native_identity import read_owner
        from ao_native_outcome import validate_source, source_keys
        owner = read_owner(ao_database_path, request['session_id'])
        from ao_review_extension import guard_native_owner
        guard_native_owner(service, state, owner, role=role)
        source = {'database': ao_database_path, 'transcript': native_transcript_path,
                  'session_id': request['session_id'], 'native_session_id': owner['provider_conversation_id']}
        if role == 'reviewer':
            source['workspace_path'] = owner['workspace_path']
        validate_source(state, source, role)
        # Validate the complete exact-turn source before persisting a path. An explicit
        # read-only audit can correct a moved source; prior outcome records retain it.
        from ao_native_outcome import inspect
        snapshot = service.identity(service.client(state), state, request)
        verified = inspect(directory, state, request, source, snapshot)
        source_verification = {'source': source, 'native_owner': owner, 'native': verified}
        state[source_keys(role)[0]] = source
    snapshot = service.identity(service.client(state), state, request)
    value = observe(service, directory, state, request, snapshot, allow_unknown_clear=True,
                    source_verification=source_verification, restored_compaction=True)
    diagnosed = native_failure_kind(value)
    completed_compaction = request['state'] == 'completed' and diagnosed == 'compaction_thrashing'
    settlement = diagnosed if request['state'] != 'completed' else None
    if settlement == 'compaction_thrashing' and request.get('role') != 'engineer':
        settlement = None
    return {'request_id': request['request_id'], 'outcome': value['outcome'],
            'outcome_sha256': request['semantic_outcome_sha256'], 'native': value['native'],
            'observation_outcome': value.get('observation_outcome'),
            'resume_eligible': (False if completed_compaction else
                                (value['outcome']['kind'] in RESUMABLE and not inconclusive(value)
                                 if request['state'] == 'completed' else settlement is not None)),
            'model_dispatch': False, 'quota_reset_established': False}


def resume(service, directory, state, request_id, outcome_sha256, resume_request_id, diagnosis, authorization):
    from ao_project_room import atomic, digest, identifier, nonempty, read
    identifier(request_id); identifier(resume_request_id)
    nonempty(diagnosis, 'diagnosis', 6000); nonempty(authorization, 'authorization', 6000)
    request = state['requests'].get(request_id)
    if not request or latest_for_role(state, request['role']) != request:
        raise RoomError('Only the latest owned result can authorize an outcome continuation')
    audit_quiet(service, directory, state, request)
    if request['state'] == 'completed':
        current = load(directory, request)
        if current and native_failure_kind(current) == 'compaction_thrashing':
            raise RoomError('AO-completed autocompact-thrashing evidence is not eligible for the completed recovery lane; only an exact failed AO transport turn can settle this native failure')
    inputs = {'version': 1, 'room_id': state['room_id'], 'blocked_request_id': request_id,
              'outcome_sha256': outcome_sha256, 'resume_request_id': resume_request_id,
              'diagnosis': diagnosis, 'authorization': authorization}
    reconciled_observation = None
    if request.get('history_reconciliation_sha256'):
        snapshot = service.identity(service.client(state), state, request)
        reconciled_observation = observe(service, directory, state, request, snapshot)
        if (request['semantic_outcome_sha256'] != outcome_sha256
                or reconciled_observation['outcome']['kind'] not in RESUMABLE or inconclusive(reconciled_observation)):
            raise RoomError('History reconciliation evidence changed; explicit outcome audit required')
    prior = release_record(directory, state, request)
    if prior:
        if 'native_failure_settlement' in prior:
            inputs['native_failure_settlement'] = prior['native_failure_settlement']
            validate_settlement(directory, request)
        if {k: v for k, v in prior.items() if k != 'previous_resume_sha256'} == inputs:
            if (inputs.get('native_failure_settlement', {}).get('kind') == 'compaction_thrashing'
                    and resume_request_id not in state['requests']):
                snapshot = service.identity(service.client(state), state, request)
                value = observe(service, directory, state, request, snapshot)
                if (request.get('semantic_outcome_sha256') != outcome_sha256
                        or native_failure_kind(value) != 'compaction_thrashing' or inconclusive(value)):
                    raise RoomError('Compaction-thrashing continuation evidence changed; a fresh audit is required')
            return {**prior, 'model_dispatch': False}
        if prior['resume_request_id'] != resume_request_id:
            raise RoomError('This outcome already has a different immutable continuation')
        inputs['previous_resume_sha256'] = request['outcome_resume_sha256']
    import ao_acceptance_extension
    ao_acceptance_extension.guard_outcome_resume(service, state, request, resume_request_id)
    if request_id == resume_request_id or resume_request_id in state['requests']:
        raise RoomError('Continuation requires one unused request identity')
    if reconciled_observation is None:
        snapshot = service.identity(service.client(state), state, request)
        value = observe(service, directory, state, request, snapshot)
    else:
        value = reconciled_observation
    if (request['semantic_outcome_sha256'] != outcome_sha256 or value['outcome']['kind'] not in RESUMABLE
            or inconclusive(value)):
        raise RoomError('Outcome evidence changed or does not establish an eligible diagnosed failure')
    if request['state'] != 'completed':
        settlement_kind = native_failure_kind(value)
        if (request['state'] not in ('uncertain', 'settled_failure')
                or settlement_kind not in ('quota_limit', 'compaction_thrashing')
                or (settlement_kind == 'compaction_thrashing' and request.get('role') != 'engineer')):
            raise RoomError('Only an exactly correlated native quota or autocompact-thrashing failure can settle an uncertain AO failed turn')
        inputs['native_failure_settlement'] = {'prior_state': 'uncertain', 'ao_state': 'failed',
                                               'receipt_sha256': request['receipt_sha256']}
        if settlement_kind == 'compaction_thrashing':
            inputs['native_failure_settlement'].update(kind='compaction_thrashing',
                                                       required_guard_sha256=_reviewed_guard_sha256())
    path = 'outcome-resumes/' + request_id + '/' + digest(inputs) + '.json'
    if (directory / path).exists() and read(directory / path) != inputs:
        raise RoomError('A different continuation intent already exists')
    atomic(directory / path, inputs)
    request.update(outcome_resume=path, outcome_resume_sha256=digest(inputs))
    if 'native_failure_settlement' in inputs:
        request['state'] = 'settled_failure'  # Original AO receipt and failure remain immutable and unusable as success.
    service.save(directory, state)
    return {**inputs, 'model_dispatch': False}


def known_compaction_turns(directory, state, snapshot):
    """A native resume summary is context, not a new user command or successful work."""
    from ao_project_room import digest
    request = latest_for_role(state, 'engineer')
    session_id = request.get('session_id') if request else None
    if (not request or not request.get('semantic_outcome')
            or not isinstance(session_id, str) or not session_id.strip()
            or snapshot.get('sessionId') != session_id
            or session_id != state.get('bindings', {}).get('engineer', {}).get('session_id')):
        # A two-role audit also inspects reviewer history. Engineer compaction
        # proofs are neither required there nor transferable to another session.
        return set()
    record = load(directory, request)
    native = record.get('native') or {}
    source = native.get('source')
    if (native.get('unknown') or not isinstance(source, dict) or source.get('session_id') != session_id
            or source != state.get('native_outcome_source')):
        return set()
    result = set()
    for proof in native.get('compaction_imports', []):
        turns = [t for t in snapshot.get('turns', []) if t.get('id') == proof['turn_id']]
        messages = [m for m in snapshot.get('messages', []) if m.get('turnId') == proof['turn_id']]
        if (len(turns) != 1 or turns[0].get('state') != 'recovered' or digest(turns[0]) != proof['turn_sha256']
                or digest(messages) != proof['messages_sha256']):
            raise RoomError('Verified native compaction import changed; re-audit before continuing')
        result.add(proof['turn_id'])
    return result


def known_task_notification_turns(directory, state, snapshot):
    """Revalidate saved engineer notification context without changing any outcome or hold."""
    from ao_native_outcome import inspect
    request = latest_for_role(state, 'engineer')
    binding = state.get('bindings', {}).get('engineer', {})
    if (state.get('workflow') != 'fable_engineering' or not request or not request.get('semantic_outcome')
            or not isinstance(request.get('session_id'), str) or not request['session_id']
            or snapshot.get('sessionId') != request['session_id'] or binding.get('session_id') != request['session_id']):
        return set()
    record = load(directory, request)
    native = record.get('native') or {}
    proofs = native.get('task_notification_imports', [])
    if not proofs:
        return set()
    source = native.get('source')
    if (native.get('unknown') or native.get('next_human_uuid') or not isinstance(source, dict)
            or source.get('session_id') != request['session_id'] or source != state.get('native_outcome_source')):
        return set()
    try:
        current = inspect(directory, state, request, source, snapshot)
        if (current.get('unknown') or current.get('next_human_uuid')
                or current.get('task_notification_imports') != proofs):
            raise RoomError('Saved notification imports no longer match the owned native source and AO history')
    except (RoomError, OSError, ValueError, KeyError, TypeError) as exc:
        raise RoomError('Verified native task-notification import changed; re-audit before continuing') from exc
    return {proof['turn_id'] for proof in proofs}


def known_context_turns(directory, state, snapshot):
    """Keep distinct audited context kinds separate from request and completion evidence."""
    return _unowned_context_turns(state, known_compaction_turns(directory, state, snapshot)
                                 | known_task_notification_turns(directory, state, snapshot))


def _unowned_context_turns(state, imports):
    """Context proof must never override any owned turn, including an earlier baseline turn."""
    owned = {request['turn_id'] for request in state['requests'].values() if request.get('turn_id')}
    if imports & owned:
        raise RoomError('Native context import contradicts an owned request turn; preserve its receipt and diagnose history')
    return imports
