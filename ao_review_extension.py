"""One explicitly authorized fourth Fable charter review, with no dispatch or renewal."""

import copy
import json
from pathlib import Path
import re
import time

import ao_delegates
import ao_native_identity
import ao_project_room as ao
import ao_workflow
from ao_reviewer_recovery import _store_once
from implementation import candidate_snapshot
from room import RoomError

BASE = 'spec-review-extension'
LIMIT = 3
MAX_RECORD_BYTES = 96_000_000
PIN_FIELDS = ('room_id', 'project_path', 'git_common_dir', 'ao_project_id', 'ao_url', 'workflow',
              'authorization', 'exception_authorization', 'bindings', 'delegate', 'preparation',
              'preparation_sha256', 'provider_transition', 'routing_adoption', 'reviewer_recovery')
# Later observations may add a hold or independently authorize its named successor.
# Their original files stay in the audit manifest; this grant never releases them.
OBSERVATIONS = {'semantic_outcome', 'semantic_outcome_sha256', 'semantic_status', 'semantic_observation_error',
                'outcome_resume', 'outcome_resume_sha256', 'model_reroute', 'reroute_evidence', 'reroute_history'}


def _hash(value, label):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise RoomError('Use the exact ' + label + ' SHA256')
    return value


def _read(path):
    try:
        return json.loads(ao_delegates.owned_bytes(path, MAX_RECORD_BYTES))
    except (OSError, ValueError, TypeError) as exc:
        raise RoomError('Review-extension evidence is unreadable or inconsistent') from exc


def _relative(directory, relative):
    if (not isinstance(relative, str) or not relative or Path(relative).is_absolute()
            or '..' in Path(relative).parts):
        raise RoomError('Review-extension evidence requires an exact room-relative path')
    return directory / relative


def _pending(directory):
    return sorted((directory / BASE / 'requests').glob('*.json'))


def guard_pending_receipts(directory, state):
    if state.get('spec_review_extension') is None and _pending(directory):
        raise RoomError('An uncommitted review-extension receipt exists; reconcile only its identical request')


def _consumptions(directory):
    return sorted((directory / BASE / 'consumption').glob('*'))


def _consumption(directory, state, reference):
    paths = _consumptions(directory)
    if not paths:
        return None
    if len(paths) != 1:
        raise RoomError('Multiple fourth-review consumption records exist; preserve them and diagnose without resending')
    path = paths[0]
    record = _read(path)
    sha256 = ao.digest(record)
    if (path.name != sha256 + '.json' or record.get('version') != 1 or record.get('room_id') != state['room_id']
            or record.get('grant_receipt_sha256') != reference['receipt_sha256']):
        raise RoomError('The immutable fourth-review consumption evidence was modified')
    original = record['request']
    request_id = ao.identifier(original['request_id'])
    current = state['requests'].get(request_id)
    if current is None:
        raise RoomError('Fourth-review consumption is durable but its request projection is missing; diagnose without resending')
    relative = str(path.relative_to(directory))
    if (original.get('state') != 'uncertain' or original.get('purpose') != 'spec_review'
            or current.get('review_extension_consumption') != relative
            or current.get('review_extension_consumption_sha256') != sha256
            or any(current.get(k) != v for k, v in original.items() if k != 'state')):
        raise RoomError('The fourth-review request identity differs from its immutable consumption evidence')
    return original


def _audit(directory, sha256):
    value = _read(directory / BASE / 'audits' / (_hash(sha256, 'review-extension audit') + '.json'))
    if ao.digest(value) != sha256 or value.get('version') != 1:
        raise RoomError('Review-extension audit was modified')
    return value


def _reviews(state):
    return sorted((r for r in state['requests'].values() if r.get('purpose') == 'spec_review'),
                  key=lambda r: r['created_order'])


def _manifest(directory, state):
    """Retain existing immutable records, including earlier failures and releases."""
    paths = {state[k] for k in ('spec', 'preparation', 'handoff', 'checkpoint') if state.get(k)}
    for request in state['requests'].values():
        ao_workflow.completed_receipt(directory, request)
        for key in ('receipt', 'semantic_outcome', 'outcome_resume', 'response_normalization',
                    'completion_candidate', 'engineering_record', 'reroute_evidence'):
            if request.get(key):
                paths.add(request[key])
        paths.update(request.get('receipt_history', []))
        paths.update(request.get('reroute_history', []))
        ao_workflow.spec_record_file(directory, request['spec_record_sha256'])
        for folder in ('receipts', 'outcomes', 'outcome-resumes'):
            paths.update(str(p.relative_to(directory)) for p in
                         (directory / folder / ao.identifier(request['request_id'])).glob('*.json'))
    paths.update(str(p.relative_to(directory)) for p in (directory / 'specs').glob('*.json'))
    for verification in state['verifications']:
        if verification.get('path'):
            paths.add(verification['path'])
            checkpoint = _read(_relative(directory, verification['path']))
            for gate in checkpoint['gates']:
                path = _relative(directory, gate['log'])
                if ao.digest(ao_delegates.owned_bytes(path)) != gate['log_sha256']:
                    raise RoomError('Retained verification evidence was modified')
                paths.add(gate['log'])
    return {path: ao.digest(ao_delegates.owned_bytes(_relative(directory, path), 96_000_000))
            for path in sorted(paths)}


def _acceptance(directory, state, candidate_sha256, actual):
    if not state['acceptances'] or state['acceptances'][-1]['candidate_sha256'] != candidate_sha256:
        raise RoomError('Use the exact retained accepted candidate identity')
    accepted = state['acceptances'][-1]
    request = state['requests'].get(accepted['request_id'])
    if not request or request.get('role') != 'reviewer':
        raise RoomError('Retained acceptance has no original independent review')
    receipt = ao_workflow.completed_receipt(directory, request)
    verdict = ao_workflow.final_json(directory, request)
    expected = {key: accepted[key] for key in ('spec_sha256', 'candidate_sha256', 'evidence_sha256')}
    if (accepted['receipt_sha256'] != ao.digest(receipt) or request['review'] != expected
            or request['session_id'] != accepted['reviewer_session'] or request['model'] != accepted['reviewer_model']
            or verdict.get('decision') != 'approved' or any(verdict.get(k) != v for k, v in expected.items())
            or verdict.get('review') != accepted['review']):
        raise RoomError('Retained acceptance or its original verdict was modified')
    matches = []
    for verification in state['verifications']:
        if verification.get('path'):
            checkpoint = _read(_relative(directory, verification['path']))
            if ao.digest(checkpoint) == accepted['evidence_sha256']:
                matches.append((verification['path'], checkpoint))
    if len(matches) != 1:
        raise RoomError('Retained acceptance requires its exact archived checkpoint')
    path, checkpoint = matches[0]
    if (checkpoint.get('passed') is not True or checkpoint['candidate_sha256'] != candidate_sha256
            or checkpoint['spec_sha256'] != accepted['spec_sha256']
            or candidate_snapshot(actual) != checkpoint['candidate']
            or str(actual) != checkpoint['candidate_path']):
        raise RoomError('The accepted candidate or archived verification identity changed')
    return {'candidate_sha256': candidate_sha256, 'checkpoint': path, 'acceptance': accepted}


def _owner(state, database, native_session_id, workspace):
    try:
        owner = ao_native_identity.read_owner(database, state['bindings']['engineer']['session_id'])
    except (OSError, ValueError) as exc:
        raise RoomError('Retained native owner evidence is unavailable or contradictory') from exc
    engineer = state['bindings']['engineer']
    source = state.get('native_outcome_source')
    if (owner['provider_conversation_id'] != native_session_id or owner['project_id'] != state['ao_project_id']
            or owner['workspace_path'] != str(workspace) or owner['ao_conversation_id'] != engineer['conversation_id']
            or owner['active_branch_id'] != engineer['branch_id']
            or (source and (source['database'] != database or source['native_session_id'] != native_session_id
                           or source['session_id'] != engineer['session_id']))):
        raise RoomError('Native owner differs from the retained room/session/workspace evidence')
    # Runtime liveness and generation are observations, not replacement-session authority.
    return {k: v for k, v in owner.items() if k not in ('activity_state', 'controller_generation')}


def _native_evidence(directory, state, binding, snapshot):
    from ao_provider_transition import _native
    return {**_native(state, binding, snapshot, directory),
            'activities_sha256': ao.digest(snapshot['activities'])}


def _inspect(service, directory, state, target, reconcile=False):
    service.settled(state, pending_review_extension=reconcile)
    if not ao_workflow.normal(state) or state.get('spec_review_extension') is not None:
        raise RoomError('Only one review extension is available for a normal Fable room, ever')
    if not reconcile and _pending(directory):
        raise RoomError('An uncommitted review-extension receipt exists; reconcile only its identical request')
    reviews = _reviews(state)
    engineer = state['bindings'].get('engineer')
    if (len(reviews) != LIMIT or not engineer or engineer.get('harness') != 'claude-code'
            or engineer.get('model') != ao_workflow.FABLE_MODEL or engineer.get('reasoning_effort') != 'max'
            or any(r.get('role') != 'engineer' or r['session_id'] != engineer['session_id'] for r in reviews)):
        raise RoomError('The one extension requires exactly three retained Fable review intents at MAX')
    spec = service.spec(directory, state)
    prior_revision = max(ao_workflow.spec_record_file(directory, r['spec_record_sha256'])['revision'] for r in reviews)
    if (spec['revision'] != target['spec_revision'] or spec['sha256'] != target['spec_sha256']
            or spec['revision'] != prior_revision + 1):
        raise RoomError('Register the exact next charter revision before its review-extension audit')
    if state.get('executable_binding') or list((directory / 'executable-bindings').glob('*.json')):
        import ao_executable_binding
        # A grant cannot change the state needed to reconcile a pending repair.
        ao_executable_binding._chain(directory, state, _read(directory / state['preparation']))
    actual = ao_workflow.workspace(service, directory, state, check_routing=False)
    ao_delegates.validate_provider(directory, state)
    ao_delegates.assert_settled(service.root.parent, copy.deepcopy(state), directory)
    from ao_provider_transition import _CompleteClient, _ReadOnlyIdentity
    client, observer = _CompleteClient(service.client(state)), _ReadOnlyIdentity(service)
    snapshots = {role: observer.identity(client, state, binding) for role, binding in state['bindings'].items()}
    native = {role: _native_evidence(directory, state, state['bindings'][role], snapshot)
              for role, snapshot in snapshots.items()}
    owner = _owner(state, target['native_owner_database'], target['native_session_id'], actual)
    retained = _acceptance(directory, state, target['retained_candidate_sha256'], actual)
    evidence = {'state_sha256': ao.digest(state), 'state': copy.deepcopy(state), 'target': target,
                'spec_record_sha256': state['spec_record_sha256'], 'retained': retained,
                'native': native, 'native_owner': owner, 'manifest': _manifest(directory, state)}
    return evidence, snapshots


def audit(service, room_id, spec_revision, spec_sha256, retained_candidate_sha256,
          native_session_id, native_owner_database):
    if type(spec_revision) is not int or spec_revision < 2:
        raise RoomError('Use the exact next charter revision')
    _hash(spec_sha256, 'charter'); _hash(retained_candidate_sha256, 'accepted candidate')
    ao.nonempty(native_session_id, 'native_session_id', 160)
    ao.nonempty(native_owner_database, 'native_owner_database')
    target = {'spec_revision': spec_revision, 'spec_sha256': spec_sha256,
              'retained_candidate_sha256': retained_candidate_sha256,
              'native_session_id': native_session_id, 'native_owner_database': native_owner_database}
    with service.locked(room_id) as (directory, state):
        evidence, snapshots = _inspect(service, directory, state, target)
        value = {'version': 1, 'evidence': evidence, 'observed_snapshots': snapshots, 'observed_at': time.time()}
        sha256 = ao.digest(value)
        # Match _store_once's JSON formatting, UTF-8 bytes and trailing newline.
        # Keep both complete raw snapshots; an oversized audit is never published.
        serialized = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode('utf-8') + b'\n'
        if len(serialized) > MAX_RECORD_BYTES:
            raise RoomError('Complete review-extension audit exceeds its readable size bound; no audit was published')
        _store_once(directory / BASE / 'audits' / (sha256 + '.json'), value)
        return {'eligible': True, 'audit_sha256': sha256, 'room_id': room_id, **target,
                'spec_record_sha256': state['spec_record_sha256'], 'prior_spec_review_attempts': LIMIT,
                'additional_spec_reviews': 1, 'model_dispatch': False}


def _retained(directory, state, evidence):
    baseline = evidence['state']
    if ao.digest(baseline) != evidence['state_sha256']:
        raise RoomError('Review-extension original state evidence was modified')
    if any(state.get(k) != baseline.get(k) for k in PIN_FIELDS):
        raise RoomError('Review-extension room, native binding, authorization or pinned metadata changed')
    original_amendments = baseline.get('instruction_amendments', [])
    current_amendments = state.get('instruction_amendments', [])
    if (not isinstance(current_amendments, list)
            or current_amendments[:len(original_amendments)] != original_amendments):
        raise RoomError('Review-extension original instruction amendments were removed, reordered or changed')
    from ao_instruction_amendments import pending
    pending(directory, state, baseline['bindings']['engineer']['session_id'])  # Validates original and appended immutable bytes.
    _source_record(directory, state, evidence)
    if (state.get('executable_binding') or baseline.get('executable_binding')
            or list((directory / 'executable-bindings').glob('*.json'))):
        import ao_executable_binding
        prepared = _read(directory / state['preparation'])
        ao_executable_binding._chain(directory, state, prepared)
        pointer = state.get('executable_binding')
        while pointer != baseline.get('executable_binding'):
            if pointer is None:
                raise RoomError('Review-extension original executable binding is missing from its journal')
            record = ao_executable_binding._read(directory, pointer)
            _same_owner(record['evidence']['native_owner'], evidence)
            pointer = record['previous']
    for key in ('acceptances', 'verifications'):
        if state[key][:len(baseline[key])] != baseline[key]:
            raise RoomError('Review-extension retained acceptance or verification history changed')
    for request_id, original in baseline['requests'].items():
        current = state['requests'].get(request_id)
        if not current or any(current.get(k) != v for k, v in original.items() if k not in OBSERVATIONS):
            raise RoomError('Review-extension prior requests, receipts or counters changed')
    for path, expected in evidence['manifest'].items():
        if ao.digest(ao_delegates.owned_bytes(_relative(directory, path), 96_000_000)) != expected:
            raise RoomError('Review-extension retained evidence is missing or modified')
    ao_workflow.spec_record_file(directory, evidence['spec_record_sha256'])


def _same_owner(owner, evidence):
    if (not isinstance(owner, dict)
            or {k: v for k, v in owner.items() if k not in ('activity_state', 'controller_generation')}
            != evidence['native_owner']):
        raise RoomError('The committed fourth-review grant requires the same native owner; replacement was not recorded')


def _source_record(directory, state, evidence):
    source = state.get('native_outcome_source')
    if source == evidence['state'].get('native_outcome_source'):
        return
    pointer = state.get('native_outcome_source_evidence')
    if not isinstance(source, dict) or not isinstance(pointer, dict) or set(pointer) != {'path', 'sha256', 'request_id'}:
        raise RoomError('Changed native outcome source requires its saved verified source audit')
    request = state['requests'][pointer['request_id']]
    value = _read(_relative(directory, pointer['path']))
    from ao_outcomes import load
    load(directory, {**request, 'semantic_outcome': pointer['path'], 'semantic_outcome_sha256': pointer['sha256'],
                     'semantic_status': value['outcome']})
    proof = value.get('native_source_verification') or {}
    if not isinstance(proof, dict):
        raise RoomError('Native source audit has malformed verification evidence')
    native = proof.get('native') or {}
    if (not isinstance(native, dict) or proof.get('source') != source or native.get('source') != source or native.get('unknown')
            or not native.get('anchor_uuid') or not native.get('source_sha256')
            or request.get('role') != 'engineer' or request.get('session_id') != source.get('session_id')
            or source.get('session_id') != evidence['state']['bindings']['engineer']['session_id']
            or source.get('native_session_id') != evidence['target']['native_session_id']):
        raise RoomError('Native source audit does not prove the grant\'s retained owner')
    _same_owner(proof['native_owner'], evidence)


def _anchor(directory, state):
    reference = state['spec_review_extension']
    record = _read(_relative(directory, reference['receipt']))
    if ao.digest(record) != reference['receipt_sha256'] or record['room_id'] != state['room_id']:
        raise RoomError('Committed review-extension grant was modified')
    evidence = _audit(directory, record['inputs']['audit_sha256'])['evidence']
    if record['evidence_sha256'] != ao.digest(evidence):
        raise RoomError('Review-extension audit chain changed')
    return evidence


def guard_native_owner(service, state, owner):
    """A source-path audit or executable repair may retain, never replace, this owner."""
    if state.get('spec_review_extension'):
        _same_owner(owner, _anchor(service.root / 'rooms' / state['room_id'], state))


def guard_replacement(state, operation):
    if state.get('spec_review_extension'):
        raise RoomError('The committed fourth-review grant pins provider and routing identity; ' + operation
                        + ' is unsupported after that grant. No transition intent was written')


def validate(service, state, allow_pending=False):
    """Offline integrity and accounting only; available never means a provider hold cleared."""
    directory = service.root / 'rooms' / state['room_id']
    reference = state.get('spec_review_extension')
    if reference is None:
        if _consumptions(directory):
            raise RoomError('Fourth-review consumption evidence has no committed grant; diagnose without resending')
        if not allow_pending:
            guard_pending_receipts(directory, state)
        return None
    try:
        request_id = ao.identifier(reference['request_id'])
        relative = BASE + '/requests/' + request_id + '.json'
        if reference['receipt'] != relative or _pending(directory) != [directory / relative]:
            raise RoomError('Review-extension receipt path or single-grant identity changed')
        record = _read(directory / relative)
        if (ao.digest(record) != reference['receipt_sha256'] or record['room_id'] != state['room_id']
                or record['inputs']['request_id'] != request_id or ao.digest(record['inputs']) != reference['key']
                or record.get('additional_spec_reviews') != 1 or record.get('maximum_spec_review_attempts') != LIMIT + 1):
            raise RoomError('Committed review-extension grant was modified')
        saved = _audit(directory, record['inputs']['audit_sha256'])
        evidence = saved['evidence']
        if (record['evidence_sha256'] != ao.digest(evidence) or record['before_state_sha256'] != evidence['state_sha256']
                or len(_reviews(evidence['state'])) != LIMIT):
            raise RoomError('Review-extension audit chain changed')
        _retained(directory, state, evidence)
        consumed = _consumption(directory, state, reference)
        reviews = _reviews(state)
        if not LIMIT <= len(reviews) <= LIMIT + 1:
            raise RoomError('Review-extension attempts were removed or exceeded the single fourth intent')
        claims = [r for r in state['requests'].values() if (r.get('carried') or {}).get('spec_review_extension_sha256')]
        if len(reviews) == LIMIT + 1:
            last = reviews[-1]
            if (consumed is None or consumed['request_id'] != last['request_id']
                    or claims != [last] or last['spec_record_sha256'] != evidence['spec_record_sha256']
                    or last['role'] != 'engineer' or last['session_id'] != evidence['state']['bindings']['engineer']['session_id']
                    or last['carried']['spec_review_extension_sha256'] != reference['receipt_sha256']
                    or any(last.get(k) != v for k, v in evidence['state']['bindings']['engineer'].items())):
                raise RoomError('The fourth review intent has no exact matching one-time grant')
        elif claims or consumed:
            raise RoomError('A review-extension claim was relabeled or removed from review accounting')
        return record, evidence
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise RoomError('Review-extension evidence is unreadable or inconsistent') from exc


def _result(state, record):
    reviews = _reviews(state)
    return {'extended': True, 'request_id': record['inputs']['request_id'], 'additional_spec_reviews': 1,
            'maximum_spec_review_attempts': LIMIT + 1, 'remaining_spec_reviews': int(len(reviews) == LIMIT),
            'consumed_by': reviews[-1]['request_id'] if len(reviews) > LIMIT else None,
            'audit_sha256': record['inputs']['audit_sha256'],
            'receipt': state['spec_review_extension']['receipt'],
            'receipt_sha256': state['spec_review_extension']['receipt_sha256'], 'model_dispatch': False,
            'meaning': 'One fourth charter-review intent only; source-review and acceptance allowances and all holds stay unchanged'}


def consume(service, directory, state, request):
    """Save a one-time immutable send intent before its state projection or any AO POST.

    An interrupted projection deliberately has no resend/reconstruction lane.
    The orphaned record remains a diagnosed hold even if native history is empty.
    """
    committed = validate(service, state)
    if not committed or len(_reviews(state)) != LIMIT:
        raise RoomError('An unused exact one-time review grant is required before consuming its fourth intent')
    _, evidence = committed
    reference = state['spec_review_extension']
    if (request['request_id'] in state['requests'] or request.get('purpose') != 'spec_review'
            or request.get('role') != 'engineer' or request.get('state') != 'uncertain'
            or request.get('spec_record_sha256') != evidence['spec_record_sha256']
            or (request.get('carried') or {}).get('spec_review_extension_sha256') != reference['receipt_sha256']
            or any(request.get(k) != v for k, v in state['bindings']['engineer'].items())):
        raise RoomError('Only the exact new fourth charter-review intent may consume this grant')
    record = {'version': 1, 'room_id': state['room_id'], 'grant_receipt_sha256': reference['receipt_sha256'],
              'request': copy.deepcopy(request)}
    sha256 = ao.digest(record)
    relative = BASE + '/consumption/' + sha256 + '.json'
    _store_once(directory / relative, record)
    request.update(review_extension_consumption=relative, review_extension_consumption_sha256=sha256)


def extend(service, room_id, audit_sha256, authorization, diagnosis, request_id):
    ao.identifier(request_id); _hash(audit_sha256, 'review-extension audit')
    ao.nonempty(authorization, 'authorization: actual new user answer and its approval context')
    ao.nonempty(diagnosis, 'diagnosis')
    inputs = {'request_id': request_id, 'audit_sha256': audit_sha256,
              'authorization': authorization, 'diagnosis': diagnosis}
    with service.locked(room_id) as (directory, state):
        committed = validate(service, state, allow_pending=True)
        if committed:
            record, _ = committed
            if record['inputs'] != inputs:
                raise RoomError('The one review extension already belongs to another payload; it cannot renew or renumber')
            return _result(state, record)
        pending = _pending(directory)
        if len(pending) > 1:
            raise RoomError('Multiple review-extension receipts exist; preserve them and diagnose')
        prior = _read(pending[0]) if pending else None
        if prior and (pending[0].name != request_id + '.json' or prior.get('inputs') != inputs):
            raise RoomError('An uncommitted review extension belongs to another payload')
        saved = _audit(directory, audit_sha256)
        evidence, _ = _inspect(service, directory, state, saved['evidence']['target'], reconcile=bool(prior))
        if evidence != saved['evidence']:
            raise RoomError('Review-extension audit is stale; charter, retained evidence or native metadata changed')
        record = {'version': 1, 'room_id': room_id, 'inputs': inputs, 'additional_spec_reviews': 1,
                  'maximum_spec_review_attempts': LIMIT + 1, 'before_state_sha256': evidence['state_sha256'],
                  'evidence_sha256': ao.digest(evidence), 'recorded_at': prior['recorded_at'] if prior else time.time()}
        if prior and prior != record:
            raise RoomError('Pending review-extension receipt differs from the recomputed grant')
        repeated, _ = _inspect(service, directory, state, evidence['target'], reconcile=bool(prior))
        if repeated != evidence:
            raise RoomError('Review-extension evidence changed before commit')
        relative = BASE + '/requests/' + request_id + '.json'
        if not prior:
            _store_once(directory / relative, record)
        state['spec_review_extension'] = {'request_id': request_id, 'key': ao.digest(inputs),
                                          'receipt': relative, 'receipt_sha256': ao.digest(record)}
        service.save(directory, state)
        return _result(state, record)


def admission(service, directory, state):
    committed = validate(service, state)
    count = len(_reviews(state))
    if count < LIMIT and not committed:
        return None
    if not committed:
        raise RoomError('Three Fable spec-review attempts exhausted; preserve evidence and surface the user decision')
    if count >= LIMIT + 1:
        raise RoomError('The one fourth Fable review intent is exhausted, including failed or uncertain delivery')
    _, evidence = committed
    spec = service.spec(directory, state)
    target = evidence['target']
    if (state['spec_record_sha256'] != evidence['spec_record_sha256']
            or spec['revision'] != target['spec_revision'] or spec['sha256'] != target['spec_sha256']):
        raise RoomError('The review extension belongs only to its exact audited next charter')
    actual = ao_workflow.workspace(service, directory, state, check_routing=False)
    _acceptance(directory, state, target['retained_candidate_sha256'], actual)
    database = (state.get('native_outcome_source') or {}).get('database', target['native_owner_database'])
    if _owner(state, database, target['native_session_id'], actual) != evidence['native_owner']:
        raise RoomError('The retained native owner changed after the review extension')
    from ao_provider_transition import _CompleteClient, _ReadOnlyIdentity
    client, observer = _CompleteClient(service.client(state)), _ReadOnlyIdentity(service)
    for role, binding in state['bindings'].items():
        snapshot = observer.identity(client, state, binding)
        if _native_evidence(directory, state, binding, snapshot) != evidence['native'][role]:
            raise RoomError('Native history or metadata changed after the review extension')
    return state['spec_review_extension']['receipt_sha256']


def summary(service, state):
    committed = validate(service, state, allow_pending=True)
    if committed:
        return _result(state, committed[0])
    if _pending(service.root / 'rooms' / state['room_id']):
        return {'state': 'pending_uncommitted', 'model_dispatch': False,
                'meaning': 'A durable grant receipt exists; only the identical extend request may reconcile it'}
    return None
