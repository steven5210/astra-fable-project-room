"""One explicitly authorized exact-spec re-review after a consumed charter extension, with no renewal.

This is a separate, once-per-room continuation -- not an extension of the ordinary review limit
and not a refund of the consumed fourth charter review. It applies only when the room's one
charter-review extension was granted and consumed, that fourth review completed with a retained
negative or noncanonical verdict, the operator registered the exact corrected next revision, and
the actual human answer authorized one more exact-spec review. One named intent is consumed
durably before POST; failed or uncertain delivery exhausts it. There is no banking, no chaining
and no counter; any further substantive review needs a new product decision.
"""

import copy
import json
import os
from pathlib import Path
import re
import stat
import time

import ao_delegates
import ao_project_room as ao
import ao_review_extension as extension
import ao_workflow
from ao_reviewer_recovery import _store_once
from implementation import candidate_snapshot
from room import RoomError

BASE = 'spec-rereview'
KEY = 'spec_rereview'
LANE = 'spec_rereview'
CLAIM = 'spec_rereview_sha256'
MEANING = ('One authorized exact-spec re-review intent only, of the exact corrected charter; the consumed '
           'fourth review, its verdict and every other allowance stay unchanged')
MAX_RECORD_BYTES = 96_000_000
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
# Mechanical source-result kinds map to exactly the operator-declared classifications.
CLASSIFICATIONS = {'changes_required': 'changes_required', 'accept_with_blockers': 'accept_with_blockers',
                   'noncanonical_object': 'noncanonical_negative'}
EXHAUSTED = 'The one re-review intent is exhausted, including failed or uncertain delivery'


def _hash(value, label):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise RoomError('Use the exact ' + label + ' SHA256')
    return value


def _read(path):
    try:
        return json.loads(ao_delegates.owned_bytes(path, MAX_RECORD_BYTES))
    except (OSError, ValueError, TypeError) as exc:
        raise RoomError('Re-review evidence is unreadable or inconsistent') from exc


def _relative(directory, relative):
    if (not isinstance(relative, str) or not relative or Path(relative).is_absolute()
            or '..' in Path(relative).parts):
        raise RoomError('Re-review evidence requires an exact room-relative path')
    return directory / relative


def _pending(directory):
    return sorted((directory / BASE / 'requests').glob('*.json'))


def guard_pending_receipts(directory, state):
    if state.get(KEY) is None and _pending(directory):
        raise RoomError('An uncommitted re-review receipt exists; reconcile only its identical request')


def _consumptions(directory):
    return sorted((directory / BASE / 'consumption').glob('*'))


def _consumption(directory, state, reference):
    paths = _consumptions(directory)
    if not paths:
        return None
    if len(paths) != 1:
        raise RoomError('Multiple re-review consumption records exist; preserve them and diagnose without resending')
    path = paths[0]
    record = _read(path)
    sha256 = ao.digest(record)
    if (path.name != sha256 + '.json' or record.get('version') != 1 or record.get('room_id') != state['room_id']
            or record.get('grant_receipt_sha256') != reference['receipt_sha256']):
        raise RoomError('The immutable re-review consumption evidence was modified')
    original = record['request']
    request_id = ao.identifier(original['request_id'])
    current = state['requests'].get(request_id)
    if current is None:
        raise RoomError('Re-review consumption is durable but its request projection is missing; diagnose without resending')
    relative = str(path.relative_to(directory))
    if (original.get('state') != 'uncertain' or original.get('purpose') != 'spec_review'
            or current.get('spec_rereview_consumption') != relative
            or current.get('spec_rereview_consumption_sha256') != sha256
            or any(current.get(k) != v for k, v in original.items() if k != 'state')):
        raise RoomError('The re-review request identity differs from its immutable consumption evidence')
    return original


def _audit(directory, sha256):
    value = _read(directory / BASE / 'audits' / (_hash(sha256, 're-review audit') + '.json'))
    if ao.digest(value) != sha256 or value.get('version') != 1:
        raise RoomError('Re-review audit was modified')
    return value


def _claims(state):
    return [r for r in state['requests'].values() if (r.get('carried') or {}).get(CLAIM)]


def _artifact_bytes(path):
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise RoomError('Re-review gate artifact is missing or unreadable') from exc
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise RoomError('Re-review gate artifact must be one regular file without links')
    try:
        return ao_delegates.owned_bytes(path, MAX_ARTIFACT_BYTES)
    except (OSError, ValueError) as exc:
        raise RoomError('Re-review gate artifact is missing, unsafe or oversized') from exc


def _gate_artifacts(spec, artifacts):
    """Bind each declared absolute artifact path to the digest of its live bytes.

    Every artifact must appear verbatim as an argv token of at least one registered gate; when the
    argv token immediately after it is a 64-hex string, that declared hash must equal the artifact
    bytes wherever it appears.
    """
    if (not isinstance(artifacts, list)
            or any(not isinstance(path, str) or not os.path.isabs(path) for path in artifacts)):
        raise RoomError('Re-review gate artifacts must be a list of absolute paths')
    bound = {}
    for path in artifacts:
        positions = [(gate, i) for gate in spec['gates'] for i, token in enumerate(gate) if token == path]
        if not positions:
            raise RoomError('A re-review gate artifact is absent from every registered gate argv')
        sha256 = ao.digest(_artifact_bytes(path))
        for gate, i in positions:
            following = gate[i + 1] if i + 1 < len(gate) else None
            if isinstance(following, str) and re.fullmatch('[0-9a-f]{64}', following) and following != sha256:
                raise RoomError('A re-review gate artifact declared hash differs from its bytes')
        bound[path] = sha256
    return bound


def _source_kind(directory, source):
    """Mechanical classification of the retained source result; the original verdict is never reinterpreted."""
    from ao_response_normalization import ResponseFormatError
    try:
        value = ao_workflow.final_json(directory, source)
    except ResponseFormatError as exc:
        raise RoomError('A prose-only or multi-object source result is not a completed negative verdict; '
                        'use the supported format-recovery or normalization lanes') from exc
    if not isinstance(value, dict):
        raise RoomError('The source result is not one JSON object; use the supported recovery lanes')
    findings = value.get('findings')
    str_findings = isinstance(findings, list) and all(isinstance(f, str) for f in findings)
    blockers = bool(str_findings and any(f.startswith('BLOCKER:') for f in findings))
    decision = value.get('decision')
    if decision == 'changes_required' and str_findings:
        kind = 'changes_required'
    elif decision == 'accept' and str_findings and blockers:
        kind = 'accept_with_blockers'
    elif decision == 'accept' and str_findings:
        raise RoomError('The source review accepted its specification; no re-review continuation applies')
    else:
        kind = 'noncanonical_object'
    return value, kind


def _inspect(service, directory, state, target, reconcile=False):
    service.settled(state, pending_spec_rereview=reconcile)
    if not ao_workflow.normal(state) or state.get(KEY) is not None:
        raise RoomError('Only one re-review continuation is available for a normal Fable room, ever')
    if not reconcile and _pending(directory):
        raise RoomError('An uncommitted re-review receipt exists; reconcile only its identical request')
    committed = extension.validate(service, state)
    if not committed:
        raise RoomError('Re-review requires the consumed one-time charter review extension')
    ext_record, ext_evidence = committed
    ext_reference = state['spec_review_extension']
    consumed = extension._consumption(directory, state, ext_reference)
    if consumed is None or len(extension._reviews(state)) != extension.LIMIT + 1:
        raise RoomError('Re-review requires the charter extension consumed by exactly one completed fourth review')
    import ao_outcomes
    source = state['requests'].get(target['source_request_id'])
    if (source is None or source is not ao_workflow.latest(state, {'spec_review'})
            or source is not ao_outcomes.latest_for_role(state, 'engineer')
            or consumed['request_id'] != source['request_id']
            or source.get('state') != 'completed' or source.get('role') != 'engineer'):
        raise RoomError('Re-review requires the consumed fourth review as its source, still the latest '
                        'retained engineer result')
    ao_workflow.completed_receipt(directory, source)
    from ao_model_boundaries import _authenticated_request
    _authenticated_request(directory, source)
    if source.get('receipt_sha256') != target['source_receipt_sha256']:
        raise RoomError('The source review receipt differs from the audited re-review source')
    if source.get('semantic_observation_error'):
        raise RoomError('The source review has a semantic observation error; resolve it before any re-review')
    ao_outcomes.usable(directory, source)
    raw = ao_workflow.raw_final_text(directory, source)
    if ao.digest(raw.encode()) != target['source_final_text_sha256']:
        raise RoomError('The source review final text differs from the audited re-review source')
    if source['spec_record_sha256'] != ext_evidence['spec_record_sha256']:
        raise RoomError('The source review did not review the charter extension\'s audited specification')
    value, kind = _source_kind(directory, source)
    if CLASSIFICATIONS[kind] != target['source_classification']:
        raise RoomError('The declared source classification does not match the retained source result')
    try:
        ao_workflow.agreement(service, directory, state)
    except RoomError as exc:
        agreement_refusal = str(exc)
    else:
        raise RoomError('The current exact specification is already agreed; no re-review continuation applies')
    spec = service.spec(directory, state)
    source_spec = ao_workflow.spec_record_file(directory, source['spec_record_sha256'])
    if (spec['revision'] != target['spec_revision'] or spec['sha256'] != target['spec_sha256']
            or spec['revision'] != source_spec['revision'] + 1
            or state['spec_record_sha256'] == source['spec_record_sha256']):
        raise RoomError('Register the exact corrected next charter revision before the re-review audit')
    actual = ao_workflow.workspace(service, directory, state, check_routing=True)
    artifacts = _gate_artifacts(spec, target['gate_artifacts'])
    candidate = candidate_snapshot(actual)
    lane = ext_record.get('lane', extension.ACCEPTED)
    policy = ao_delegates.validate_provider(directory, state)
    ao_delegates.assert_settled(service.root.parent, copy.deepcopy(state), directory)
    owner = extension._owner(state, target['native_owner_database'], target['native_session_id'], actual)
    from ao_provider_transition import _CompleteClient, _ReadOnlyIdentity
    client, observer = _CompleteClient(service.client(state)), _ReadOnlyIdentity(service)
    snapshots = {role: observer.identity(client, state, binding) for role, binding in state['bindings'].items()}
    native = {role: extension._native_evidence(directory, state, state['bindings'][role], snapshot,
                                             settled_history=(lane == extension.PRE and role == 'engineer'))
              for role, snapshot in snapshots.items()}
    settled_history = lane == extension.PRE and bool(extension._settled_requests(state, state['bindings']['engineer']))
    manifest = extension._grant_manifest(directory, state, target['native_session_id'],
                                       settled_history=settled_history)
    prepared = ao_delegates.validate_preparation(directory, state, state['bindings']['engineer']['session_id'])
    contract = ao_workflow.part_texts(prepared, policy)['review_contract']
    if target['request_id'] in state['requests']:
        raise RoomError('The named re-review request identity already exists')
    evidence = {'state_sha256': ao.digest(state), 'state': copy.deepcopy(state), 'target': target,
                'extension': {'receipt_sha256': ext_reference['receipt_sha256'], 'lane': lane,
                              'consumer_request_id': consumed['request_id']},
                'source': {'request_id': source['request_id'], 'receipt_sha256': source['receipt_sha256'],
                           'spec_record_sha256': source['spec_record_sha256'], 'state': source['state'],
                           'final_text_sha256': target['source_final_text_sha256'],
                           'result_sha256': ao.digest(value), 'kind': kind,
                           'classification': target['source_classification'],
                           'canonical_decision_present': kind != 'noncanonical_object',
                           'agreement_refusal': agreement_refusal},
                'spec': {'record_sha256': state['spec_record_sha256'], 'revision': spec['revision'],
                         'sha256': spec['sha256'], 'gates_sha256': ao.digest(spec['gates'])},
                'gate_artifacts': artifacts,
                'candidate': {'sha256': candidate['sha256'], 'path': str(actual),
                              'acceptances': len(state['acceptances']), 'verifications': len(state['verifications'])},
                'native_owner': owner, 'native': native, 'manifest': manifest,
                'review_contract_sha256': ao.digest(contract.encode())}
    return evidence, snapshots


def audit(service, room_id, spec_revision, spec_sha256, source_request_id, source_receipt_sha256,
          source_final_text_sha256, source_classification, request_id, message_sha256, gate_artifacts,
          native_session_id, native_owner_database):
    """Audit the one re-review continuation. Saves audit evidence only: no state change, request,
    model dispatch or POST. An audit alone is never an allowance."""
    if type(spec_revision) is not int or spec_revision < 2:
        raise RoomError('Use the exact corrected next charter revision')
    _hash(spec_sha256, 'corrected charter'); _hash(source_receipt_sha256, 'source review receipt')
    _hash(source_final_text_sha256, 'source final text'); _hash(message_sha256, 'caller message')
    ao.identifier(source_request_id); ao.identifier(request_id)
    if source_classification not in set(CLASSIFICATIONS.values()):
        raise RoomError('Re-review source classification must be changes_required, accept_with_blockers '
                        'or noncanonical_negative')
    if not isinstance(gate_artifacts, list) or any(not isinstance(path, str) for path in gate_artifacts):
        raise RoomError('Re-review gate artifacts must be a list of absolute paths')
    ao.nonempty(native_session_id, 'native_session_id', 160)
    ao.nonempty(native_owner_database, 'native_owner_database')
    target = {'spec_revision': spec_revision, 'spec_sha256': spec_sha256, 'source_request_id': source_request_id,
              'source_receipt_sha256': source_receipt_sha256, 'source_final_text_sha256': source_final_text_sha256,
              'source_classification': source_classification, 'request_id': request_id,
              'message_sha256': message_sha256, 'gate_artifacts': gate_artifacts,
              'native_session_id': native_session_id, 'native_owner_database': native_owner_database}
    with service.locked(room_id) as (directory, state):
        evidence, snapshots = _inspect(service, directory, state, target)
        from ao_provider_transition import projected_snapshot
        value = {'version': 1, 'evidence': evidence, 'observed_at': time.time(),
                 'observed_snapshots': {role: projected_snapshot(snapshot) for role, snapshot in snapshots.items()}}
        serialized = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode('utf-8') + b'\n'
        if len(serialized) > MAX_RECORD_BYTES:
            raise RoomError('Complete re-review audit exceeds its readable size bound; no audit was published')
        sha256 = ao.digest(value)
        _store_once(directory / BASE / 'audits' / (sha256 + '.json'), value)
        return {'eligible': True, 'audit_sha256': sha256, 'room_id': room_id, **target,
                'spec_record_sha256': state['spec_record_sha256'],
                'source_result_kind': evidence['source']['kind'],
                'gate_artifacts': evidence['gate_artifacts'],
                'review_contract_sha256': evidence['review_contract_sha256'], 'model_dispatch': False}


def _audit_history_unchanged(saved, snapshots):
    saved_snapshots = saved.get('observed_snapshots')
    if not isinstance(saved_snapshots, dict) or set(saved_snapshots) != set(snapshots):
        unchanged = False
    else:
        from ao_provider_transition import observed_history_matches
        unchanged = all(observed_history_matches(saved_snapshots.get(role), snapshot)
                        for role, snapshot in snapshots.items())
    if not unchanged:
        raise RoomError('Re-review audit observed history changed; audit again')


def _retained(service, directory, state, evidence):
    baseline = evidence['state']
    lane = evidence['extension']['lane']
    if ao.digest(baseline) != evidence['state_sha256']:
        raise RoomError('Re-review original state evidence was modified')
    # 'spec' is deliberately not pinned: after the grant is consumed, a later charter registration
    # stays legal exactly as it does for the consumed extension, and validation must keep passing.
    # The pointer fields executable_binding, native_outcome_source and native_outcome_source_evidence
    # are also not pinned by equality: a same-owner executable repair and a verified native-source
    # audit are supported operations, authenticated below exactly as the extension authenticates them.
    if ((any(state.get(k) != baseline.get(k) for k in extension.PIN_FIELDS)
            and not (lane == extension.PRE
                     and extension._recovered_reviewer(service, state, baseline)))
            or state.get('spec_review_extension') != baseline.get('spec_review_extension')):
        raise RoomError('Re-review room, charter grant, native binding, authorization or pinned metadata changed')
    original_amendments = baseline.get('instruction_amendments', [])
    current_amendments = state.get('instruction_amendments', [])
    if (not isinstance(current_amendments, list)
            or current_amendments[:len(original_amendments)] != original_amendments):
        raise RoomError('Re-review original instruction amendments were removed, reordered or changed')
    from ao_instruction_amendments import pending
    pending(directory, state, baseline['bindings']['engineer']['session_id'])
    extension._source_record(directory, state, evidence)
    if (state.get('executable_binding') or baseline.get('executable_binding')
            or list((directory / 'executable-bindings').glob('*.json'))):
        import ao_executable_binding
        prepared = _read(directory / state['preparation'])
        ao_executable_binding._chain(directory, state, prepared)
        pointer = state.get('executable_binding')
        while pointer != baseline.get('executable_binding'):
            if pointer is None:
                raise RoomError('Re-review original executable binding is missing from its journal')
            record = ao_executable_binding._read(directory, pointer)
            extension._same_owner(record['evidence']['native_owner'], evidence)
            pointer = record['previous']
    if state.get('routing_refresh') != baseline.get('routing_refresh'):
        raise RoomError('A routing refresh after the re-review grant is unsupported; '
                        'the audited routing pointer must remain')
    if baseline.get('routing_refresh') or list((directory / 'routing-refresh').glob('*.json')):
        import ao_routing_refresh
        prepared = _read(directory / state['preparation'])
        # Routing validation separately blocks every unclaimed intent and mixed
        # runtime file. Here only committed ancestry is inspected, allowing the
        # exact interrupted refresh to validate its unchanged re-review evidence.
        ao_routing_refresh._chain(directory, state, prepared, check_unclaimed=False)
    settled_history = (lane == extension.PRE
                       and bool(extension._settled_requests(baseline, baseline['bindings']['engineer'])))
    with extension._HistoryEvidence(directory, evidence['target']['native_session_id'], settled_history) as history:
        for request_id, original in baseline['requests'].items():
            current = state['requests'].get(request_id)
            if not current or any(current.get(k) != v for k, v in original.items()
                                  if k not in extension.OBSERVATIONS and k not in extension.HISTORY_POINTERS):
                raise RoomError('Re-review prior requests, receipts or counters changed')
            if any(key in original for key in extension.HISTORY_POINTERS):
                receipt = ao_workflow.completed_receipt(directory, original)
                history.retained(original, current, receipt)
        for path, expected in evidence['manifest'].items():
            if path in history.raw_hashes:
                actual = history.raw_hashes[path]
            else:
                if path.split('/')[0] in ('history-reconciliations', 'history-reconciliation-invalidations'):
                    raise RoomError('Re-review retained history manifest has an unanchored record')
                actual = ao.digest(ao_delegates.owned_bytes(_relative(directory, path), MAX_RECORD_BYTES))
            if actual != expected:
                raise RoomError('Re-review retained evidence is missing or modified')
    if settled_history:
        granted = evidence['native']['engineer'].get('settled_failures')
        pinned = {request['request_id']
                  for request in extension._settled_requests(baseline, baseline['bindings']['engineer'])}
        if (not isinstance(granted, list) or not granted or not all(isinstance(entry, dict) for entry in granted)
                or {entry.get('request_id') for entry in granted} != pinned
                or extension._settled_failures(directory, state, baseline['bindings']['engineer'],
                                               request_ids=pinned) != granted):
            raise RoomError('Re-review granted settlement evidence changed')
    for key in ('acceptances', 'verifications'):
        if state[key][:len(baseline[key])] != baseline[key]:
            raise RoomError('Re-review retained acceptance or verification history changed')
    ao_workflow.spec_record_file(directory, evidence['spec']['record_sha256'])


def validate(service, state, allow_pending=False):
    """Offline integrity and accounting only; available never means a provider hold cleared."""
    directory = service.root / 'rooms' / state['room_id']
    reference = state.get(KEY)
    if reference is None:
        if _consumptions(directory):
            raise RoomError('Re-review consumption evidence has no committed grant; diagnose without resending')
        if _claims(state):
            raise RoomError('A re-review claim exists without its committed grant')
        if not allow_pending:
            guard_pending_receipts(directory, state)
        return None
    try:
        request_id = ao.identifier(reference['request_id'])
        relative = BASE + '/requests/' + request_id + '.json'
        if reference['receipt'] != relative or _pending(directory) != [directory / relative]:
            raise RoomError('Re-review receipt path or single-grant identity changed')
        record = _read(directory / relative)
        if (ao.digest(record) != reference['receipt_sha256'] or record['room_id'] != state['room_id']
                or record['version'] != 1 or record['inputs']['request_id'] != request_id
                or ao.digest(record['inputs']) != reference['key']
                or record.get('additional_spec_reviews') != 1):
            raise RoomError('Committed re-review grant was modified')
        saved = _audit(directory, record['inputs']['audit_sha256'])
        evidence = saved['evidence']
        if (record['evidence_sha256'] != ao.digest(evidence)
                or record['before_state_sha256'] != evidence['state_sha256']
                or len(extension._reviews(evidence['state'])) != extension.LIMIT + 1):
            raise RoomError('Re-review audit chain changed')
        _retained(service, directory, state, evidence)
        consumed = _consumption(directory, state, reference)
        reviews = extension._reviews(state)
        if not extension.LIMIT + 1 <= len(reviews) <= extension.LIMIT + 2:
            raise RoomError('Re-review attempts were removed or exceeded the single authorized intent')
        claims = _claims(state)
        if len(reviews) == extension.LIMIT + 2:
            last = reviews[-1]
            import ao_engineering_model
            frozen = ao_engineering_model.binding_for_request(directory, state,
                                                            evidence['state']['bindings']['engineer'], last)
            if (consumed is None or consumed['request_id'] != last['request_id']
                    or claims != [last] or last['spec_record_sha256'] != evidence['spec']['record_sha256']
                    or last['role'] != 'engineer'
                    or last['session_id'] != evidence['state']['bindings']['engineer']['session_id']
                    or last['carried'][CLAIM] != reference['receipt_sha256']
                    or any(last.get(k) != v for k, v in frozen.items())):
                raise RoomError('The re-review intent has no exact matching one-time grant')
        elif claims or consumed or request_id in state['requests']:
            raise RoomError('A re-review claim was relabeled or removed from review accounting')
        return record, evidence
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise RoomError('Re-review evidence is unreadable or inconsistent') from exc


def _result(state, record, evidence):
    claims = _claims(state)
    return {'granted': True, 'lane': LANE, 'request_id': record['inputs']['request_id'],
            'additional_spec_reviews': 1, 'remaining_spec_reviews': int(not claims),
            'consumed_by': claims[-1]['request_id'] if claims else None,
            'audit_sha256': record['inputs']['audit_sha256'],
            'receipt': state[KEY]['receipt'], 'receipt_sha256': state[KEY]['receipt_sha256'],
            'message_sha256': evidence['target']['message_sha256'], 'model_dispatch': False,
            'meaning': MEANING}


def grant(service, room_id, audit_sha256, authorization, authorization_reference, diagnosis):
    """Commit the room's one re-review grant from its exact audit; identical calls read or reconcile it."""
    _hash(audit_sha256, 're-review audit')
    ao.nonempty(authorization, 'authorization: the actual human answer and its context')
    ao.nonempty(authorization_reference, 'authorization_reference')
    ao.nonempty(diagnosis, 'diagnosis')
    with service.locked(room_id) as (directory, state):
        committed = validate(service, state, allow_pending=True)
        if committed:
            record, evidence = committed
            inputs = record['inputs']
            if (inputs.get('audit_sha256') != audit_sha256 or inputs.get('authorization') != authorization
                    or inputs.get('authorization_reference') != authorization_reference
                    or inputs.get('diagnosis') != diagnosis):
                raise RoomError('The one re-review grant already belongs to another payload')
            return _result(state, record, evidence)
        saved = _audit(directory, audit_sha256)
        request_id = ao.identifier(saved['evidence']['target']['request_id'])
        inputs = {'request_id': request_id, 'audit_sha256': audit_sha256, 'authorization': authorization,
                  'authorization_reference': authorization_reference, 'diagnosis': diagnosis}
        pending = _pending(directory)
        if len(pending) > 1:
            raise RoomError('Multiple re-review receipts exist; preserve them and diagnose')
        prior = _read(pending[0]) if pending else None
        if prior and (pending[0].name != request_id + '.json' or prior.get('inputs') != inputs):
            raise RoomError('An uncommitted re-review grant belongs to another payload')
        full_evidence, snapshots = _inspect(service, directory, state, saved['evidence']['target'],
                                            reconcile=bool(prior))
        _audit_history_unchanged(saved, snapshots)
        evidence = full_evidence
        if ao.digest(full_evidence) != ao.digest(saved['evidence']):
            # Only an already durable receipt may reconcile across the pointer moves _retained
            # tolerates: a same-owner executable repair or a verified native-source audit after the
            # audit. The committed extension's own retained check authenticated every such move
            # inside _inspect; every other evidence byte must still be identical.
            moved = ('executable_binding', 'native_outcome_source', 'native_outcome_source_evidence')
            fresh, original = full_evidence.get('state'), saved['evidence'].get('state')
            rest = lambda value: {k: v for k, v in value.items() if k not in moved}
            if (not prior or not isinstance(fresh, dict) or not isinstance(original, dict)
                    or rest(fresh) != rest(original)
                    or {k: v for k, v in full_evidence.items() if k not in ('state', 'state_sha256')}
                    != {k: v for k, v in saved['evidence'].items() if k not in ('state', 'state_sha256')}):
                raise RoomError('Re-review audit is stale; charter, retained evidence or native metadata changed')
            evidence = saved['evidence']
        record = {'version': 1, 'room_id': room_id, 'inputs': inputs, 'additional_spec_reviews': 1,
                  'before_state_sha256': evidence['state_sha256'], 'evidence_sha256': ao.digest(evidence),
                  'recorded_at': prior['recorded_at'] if prior else time.time()}
        if prior and ao.digest(prior) != ao.digest(record):
            raise RoomError('Pending re-review receipt differs from the recomputed grant')
        repeated, snapshots_again = _inspect(service, directory, state, evidence['target'],
                                             reconcile=bool(prior))
        _audit_history_unchanged(saved, snapshots_again)
        if ao.digest(repeated) != ao.digest(full_evidence):
            raise RoomError('Re-review evidence changed before commit')
        relative = BASE + '/requests/' + request_id + '.json'
        if not prior:
            _store_once(directory / relative, record)
        reference = {'request_id': request_id, 'key': ao.digest(inputs),
                     'receipt': relative, 'receipt_sha256': ao.digest(record)}
        state[KEY] = reference
        service.save(directory, state)
        return _result(state, record, evidence)


def admission(service, directory, state, message):
    """Re-check every audited fact immediately before the one authorized intent is consumed."""
    committed = validate(service, state)
    if not committed:
        raise RoomError('An unused exact one-time re-review grant is required before its authorized intent')
    _, evidence = committed
    if len(extension._reviews(state)) >= extension.LIMIT + 2:
        raise RoomError(EXHAUSTED)
    spec = service.spec(directory, state)
    target = evidence['target']
    if (state['spec_record_sha256'] != evidence['spec']['record_sha256']
            or spec['revision'] != target['spec_revision'] or spec['sha256'] != target['spec_sha256']):
        raise RoomError('The re-review belongs only to its exact audited corrected charter')
    if ao.digest(message.encode()) != target['message_sha256']:
        raise RoomError('The re-review caller message differs from its audited exact bytes')
    actual = ao_workflow.workspace(service, directory, state, check_routing=True)
    if _gate_artifacts(spec, target['gate_artifacts']) != evidence['gate_artifacts']:
        raise RoomError('An audited re-review gate artifact changed after the grant')
    snapshot_now = candidate_snapshot(actual)
    if (snapshot_now['sha256'] != evidence['candidate']['sha256']
            or len(state['acceptances']) != evidence['candidate']['acceptances']
            or len(state['verifications']) != evidence['candidate']['verifications']):
        raise RoomError('The audited candidate or acceptance and verification accounting changed after the grant')
    database = (state.get('native_outcome_source') or {}).get('database', target['native_owner_database'])
    if extension._owner(state, database, target['native_session_id'], actual) != evidence['native_owner']:
        raise RoomError('The retained native owner changed after the re-review grant')
    lane = evidence['extension']['lane']
    from ao_provider_transition import _CompleteClient, _ReadOnlyIdentity
    client, observer = _CompleteClient(service.client(state)), _ReadOnlyIdentity(service)
    for role, binding in state['bindings'].items():
        snapshot = observer.identity(client, state, binding)
        if (extension._native_evidence(directory, state, binding, snapshot,
                                     settled_history=(lane == extension.PRE and role == 'engineer'))
                != evidence['native'][role]):
            raise RoomError('Native history or metadata changed after the re-review grant')
    ao_delegates.assert_settled(service.root.parent, copy.deepcopy(state), directory)
    import ao_outcomes
    source = state['requests'][evidence['source']['request_id']]
    if source.get('semantic_observation_error'):
        raise RoomError('The source review gained a semantic observation error after the grant')
    ao_outcomes.usable(directory, source)
    if (source.get('receipt_sha256') != evidence['source']['receipt_sha256']
            or source.get('state') != evidence['source']['state']
            or ao.digest(ao_workflow.raw_final_text(directory, source).encode())
            != evidence['source']['final_text_sha256']):
        raise RoomError('The re-review source request changed after the grant')
    policy = ao_delegates.validate_provider(directory, state)
    prepared = ao_delegates.validate_preparation(directory, state, state['bindings']['engineer']['session_id'])
    contract = ao_workflow.part_texts(prepared, policy)['review_contract']
    if ao.digest(contract.encode()) != evidence['review_contract_sha256']:
        raise RoomError('The delivered review contract differs from the audited bytes')
    return state[KEY]['receipt_sha256']


def consume(service, directory, state, request, message):
    """Save a one-time immutable send intent before its state projection or any AO POST.

    An interrupted projection deliberately has no resend/reconstruction lane.
    The orphaned record remains a diagnosed hold even if native history is empty.
    """
    committed = validate(service, state)
    if not committed or len(extension._reviews(state)) != extension.LIMIT + 1:
        raise RoomError('An unused exact one-time re-review grant is required before consuming its authorized intent')
    _, evidence = committed
    reference = state[KEY]
    carried = request.get('carried') or {}
    import ao_engineering_model
    if (request['request_id'] != reference['request_id'] or request['request_id'] in state['requests']
            or request.get('purpose') != 'spec_review' or request.get('role') != 'engineer'
            or request.get('state') != 'uncertain'
            or ao.digest(message.encode()) != evidence['target']['message_sha256']
            or request.get('spec_record_sha256') != evidence['spec']['record_sha256']
            or carried.get(CLAIM) != reference['receipt_sha256']
            or 'review_contract' not in carried.get('parts', [])
            or (carried.get('part_sha256') or {}).get('review_contract') != evidence['review_contract_sha256']
            or any(request.get(k) != v for k, v in ao_engineering_model.effective_binding(directory, state).items())):
        raise RoomError('Only the exact named re-review intent may consume this grant')
    record = {'version': 1, 'room_id': state['room_id'], 'grant_receipt_sha256': reference['receipt_sha256'],
              'request': copy.deepcopy(request)}
    sha256 = ao.digest(record)
    relative = BASE + '/consumption/' + sha256 + '.json'
    _store_once(directory / relative, record)
    request.update(spec_rereview_consumption=relative, spec_rereview_consumption_sha256=sha256)


def summary(service, state):
    """Never-raising status projection: none, pending_uncommitted, granted, consumed or damaged."""
    directory = service.root / 'rooms' / state['room_id']
    pointer = state.get(KEY)
    try:
        committed = validate(service, state, allow_pending=True)
    except (RoomError, OSError, ValueError, KeyError, TypeError) as exc:
        result = {'status': 'damaged', 'error': str(exc)[:500], 'model_dispatch': False}
        if isinstance(pointer, dict):
            result.update(request_id=pointer.get('request_id'), receipt_sha256=pointer.get('receipt_sha256'))
        return result
    if committed:
        record, _ = committed
        claims = _claims(state)
        return {'status': 'consumed' if claims else 'granted', 'request_id': record['inputs']['request_id'],
                'consumed_by': claims[-1]['request_id'] if claims else None,
                'remaining_spec_reviews': int(not claims),
                'audit_sha256': record['inputs']['audit_sha256'],
                'receipt': pointer['receipt'], 'receipt_sha256': pointer['receipt_sha256'],
                'model_dispatch': False, 'meaning': MEANING}
    if _pending(directory):
        return {'status': 'pending_uncommitted', 'model_dispatch': False,
                'meaning': 'A durable re-review receipt exists; only the identical grant request may reconcile it'}
    return None
