"""One audited lane granting exactly one named additional acceptance review after the ordinary three attempts."""

import copy
import json
import os
from pathlib import Path
import re
import time

import ao_delegates
import ao_native_identity
import ao_project_room as ao
import ao_workflow
from ao_reviewer_recovery import _store_once
from room import RoomError

BASE = 'acceptance-review-extension'
KEY = 'acceptance_review_extension'
LIMIT = ao.MAX_REVIEW_ATTEMPTS
PURPOSE = 'acceptance_review'
MAX_RECORD_BYTES = 32_000_000
EXHAUSTED = 'Three review attempts exhausted; retain evidence and surface the unresolved decision to the user'
LIVE = ('room', 'target', 'spec', 'checkpoint', 'engineering', 'reviews', 'retained_reviewer_attempts',
        'history', 'native', 'native_owner', 'workspaces')
LINKAGE = ('acceptance_review_grant', 'acceptance_review_consumption', 'acceptance_review_consumption_sha256')
OBSERVATIONS = {'semantic_outcome', 'semantic_outcome_sha256', 'semantic_status', 'semantic_observation_error',
                'outcome_resume', 'outcome_resume_sha256', 'model_reroute', 'reroute_evidence', 'reroute_history'}
# Identity that must never change, and metadata that is additionally pinned only while the latest grant is unused.
PINNED = ('room_id', 'project_path', 'git_common_dir', 'ao_project_id', 'ao_url', 'workflow')
UNUSED_PINNED = ('bindings', 'spec', 'spec_record_sha256', 'handoff', 'handoff_sha256', 'checkpoint',
                 'checkpoint_sha256', 'authorization', 'exception_authorization', 'delegate', 'preparation',
                 'preparation_sha256', 'provider_transition', 'routing_adoption', 'reviewer_recovery',
                 'spec_review_extension', 'executable_binding', 'routing_refresh')
GRANT_INPUTS = frozenset(('request_id', 'audit_sha256', 'authorization', 'authorization_reference', 'diagnosis'))
GRANT_FIELDS = frozenset(('review_request_id', 'message_sha256', 'purpose', 'role', 'additional_acceptance_reviews'))
HEX = re.compile('[0-9a-f]{64}')
IDENTIFIER = re.compile('[a-zA-Z0-9][a-zA-Z0-9_.-]{0,100}')


def _hash(value, label):
    if not isinstance(value, str) or not HEX.fullmatch(value):
        raise RoomError('Use the exact ' + label + ' SHA256')
    return value


def _is_hex(value):
    return isinstance(value, str) and bool(HEX.fullmatch(value))


def _is_identifier(value):
    return isinstance(value, str) and bool(IDENTIFIER.fullmatch(value))


def _entry_name(sequence):
    return '%06d.json' % sequence


def _entry_path(sequence):
    return BASE + '/journal/' + _entry_name(sequence)


def _roots(directory):
    """Refuse symlinked or replaced private storage before any read or durable write."""
    base = directory / BASE
    journal = base / 'journal'
    audits = base / 'audits'
    for path, label in ((base, BASE), (journal, BASE + '/journal'), (audits, BASE + '/audits')):
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise RoomError(label + ' must be a real room-private directory; symlinked or replaced storage refuses')
    return base, journal, audits


def _names(directory):
    _, journal, _ = _roots(directory)
    if not journal.exists():
        return []
    try:
        return sorted(os.listdir(journal))
    except OSError as exc:
        raise RoomError('Acceptance-review continuation evidence is unreadable or inconsistent') from exc


def _relative(directory, relative):
    if (not isinstance(relative, str) or not relative or Path(relative).is_absolute()
            or '..' in Path(relative).parts):
        raise RoomError('Acceptance-review continuation evidence requires an exact room-relative path')
    return directory / relative


def _load(path, bound=MAX_RECORD_BYTES, *, require_recorded_format=False):
    try:
        raw = ao_delegates.owned_bytes(path, bound)
        value = json.loads(raw)
        if require_recorded_format and raw != _serialized(value):
            raise RoomError('Acceptance-review continuation evidence was modified or is not in its recorded format')
        return value
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        raise RoomError('Acceptance-review continuation evidence is unreadable or inconsistent') from exc


def _serialized(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode('utf-8') + b'\n'


def _store(path, value, label):
    if len(_serialized(value)) > MAX_RECORD_BYTES:
        raise RoomError('The ' + label + ' exceeds its readable size bound; nothing was published')
    try:
        _store_once(path, value)
    except OSError as exc:
        raise RoomError('The ' + label + ' could not be stored durably; nothing was projected and the journal '
                        'must be inspected before retrying the identical request') from exc


def _linkage(request):
    return any(key in request for key in LINKAGE)


def _reviewer_requests(state):
    requests = [request for request in (state.get('requests') or {}).values() if request.get('role') == 'reviewer']
    if any(type(request.get('created_order')) is not int for request in requests):
        raise RoomError('A retained reviewer attempt has no recorded creation order; reviewer accounting is unavailable')
    return sorted(requests, key=lambda request: request['created_order'])


def _audit(directory, audit_sha256):
    _hash(audit_sha256, 'acceptance-review audit')
    _roots(directory)
    try:
        value = _load(directory / BASE / 'audits' / (audit_sha256 + '.json'), require_recorded_format=True)
        if not isinstance(value, dict) or value.get('version') != 1 or ao.digest(value) != audit_sha256:
            raise RoomError('The acceptance-review audit was modified')
    except RecursionError as exc:
        raise RoomError('Acceptance-review continuation evidence is unreadable or inconsistent') from exc
    return value


def _grant_matches_target(grant, target):
    return (isinstance(grant, dict) and isinstance(target, dict)
            and grant.get('review_request_id') == target.get('review_request_id')
            and grant.get('message_sha256') == target.get('message_sha256')
            and target.get('purpose') == PURPOSE and grant.get('purpose') == PURPOSE
            and grant.get('role') == 'reviewer' and grant.get('additional_acceptance_reviews') == 1)


def _grant_shape(entry):
    inputs, grant = entry.get('inputs'), entry.get('grant')
    if (not isinstance(inputs, dict) or set(inputs) != GRANT_INPUTS
            or not isinstance(grant, dict) or set(grant) != GRANT_FIELDS
            or not _is_identifier(inputs['request_id']) or not _is_hex(inputs['audit_sha256'])
            or any(not isinstance(inputs[key], str) or not inputs[key].strip()
                   for key in ('authorization', 'authorization_reference', 'diagnosis'))
            or not _is_identifier(grant['review_request_id']) or not _is_hex(grant['message_sha256'])
            or grant['purpose'] != PURPOSE or grant['role'] != 'reviewer'
            or grant['additional_acceptance_reviews'] != 1
            or type(entry.get('retained_reviewer_attempts')) is not int
            or not _is_hex(entry.get('evidence_sha256')) or not _is_hex(entry.get('before_state_sha256'))
            or type(entry.get('recorded_at')) not in (int, float)):
        raise RoomError('An acceptance-review grant entry is malformed')


def _consumption_shape(entry):
    request = entry.get('request')
    if (not isinstance(request, dict)
            or not _is_hex(entry.get('grant_sha256')) or not _is_identifier(entry.get('grant_request_id'))
            or not _is_identifier(entry.get('review_request_id')) or not _is_hex(entry.get('message_sha256'))
            or not isinstance(entry.get('client_message_id'), str) or not entry['client_message_id'].strip()
            or not _is_hex(entry.get('text_sha256')) or type(entry.get('recorded_at')) not in (int, float)
            or request.get('state') != 'uncertain' or request.get('role') != 'reviewer'
            or request.get('purpose') != PURPOSE or request.get('request_id') != entry['review_request_id']
            or request.get('client_message_id') != entry['client_message_id']
            or request.get('text_sha256') != entry['text_sha256']
            or not isinstance(request.get('acceptance_review_grant'), dict)
            or request['acceptance_review_grant'].get('request_id') != entry['grant_request_id']
            or request['acceptance_review_grant'].get('sha256') != entry['grant_sha256']):
        raise RoomError('An acceptance-review consumption entry is malformed')


def _shape(entry, state, index, previous):
    if (not isinstance(entry, dict) or entry.get('version') != 1 or entry.get('room_id') != state.get('room_id')
            or entry.get('sequence') != index or entry.get('previous_sha256') != previous
            or entry.get('kind') not in ('grant', 'consumption')):
        raise RoomError('An acceptance-review continuation journal entry is inconsistent with its projection or chain')
    if entry['kind'] == 'grant':
        _grant_shape(entry)
    else:
        _consumption_shape(entry)


def _journal(directory, state, names, allow_pending):
    """Parse the projected journal chain, tolerating exactly one trailing unreconciled grant receipt."""
    reference = state.get(KEY)
    if reference is None:
        projected = []
    elif (isinstance(reference, dict) and reference.get('version') == 1
          and isinstance(reference.get('entries'), list)):
        projected = reference['entries']
    else:
        raise RoomError('The acceptance-review continuation projection is malformed')
    expected = []
    for index, item in enumerate(projected, 1):
        if (not isinstance(item, dict) or item.get('sequence') != index
                or item.get('path') != _entry_path(index)
                or item.get('kind') not in ('grant', 'consumption')
                or not _is_hex(item.get('sha256'))
                or (item['kind'] == 'grant') != (index % 2 == 1)):
            raise RoomError('The acceptance-review continuation projection changed or no longer alternates grants and consumptions')
        expected.append(_entry_name(index))
    if names != expected and (len(names) != len(expected) + 1 or names[:len(expected)] != expected):
        raise RoomError('The acceptance-review continuation journal listing does not match its projected entries')
    entries, digests = [], []
    previous = None
    for index, item in enumerate(projected, 1):
        entry = _load(_relative(directory, item['path']), require_recorded_format=True)
        value = ao.digest(entry)
        if value != item['sha256']:
            raise RoomError('An acceptance-review continuation journal entry was modified')
        _shape(entry, state, index, previous)
        entries.append(entry)
        digests.append(value)
        previous = value
    pending = None
    if names != expected:
        if names[-1] != _entry_name(len(expected) + 1):
            raise RoomError('The acceptance-review continuation journal contains an unknown, renamed or reordered record')
        trailing = _load(_relative(directory, BASE + '/journal/' + names[-1]), require_recorded_format=True)
        kind = trailing.get('kind') if isinstance(trailing, dict) else None
        if kind == 'consumption':
            raise RoomError('An acceptance-review continuation consumption is durable but its request projection '
                            'is missing; diagnose without resending')
        if kind != 'grant':
            raise RoomError('The acceptance-review continuation journal contains a corrupt or unknown trailing record')
        if not allow_pending:
            raise RoomError('An uncommitted acceptance-review grant receipt exists; reconcile only its identical request')
        _shape(trailing, state, len(expected) + 1, previous)
        pending = trailing
    return entries, digests, pending


def _retained(directory, state, entries, evidence, unused):
    baseline = evidence.get('state')
    if not isinstance(baseline, dict) or evidence.get('state_sha256') != ao.digest(baseline):
        raise RoomError('The acceptance-review original state evidence was modified')
    for key in PINNED:
        if state.get(key) != baseline.get(key):
            raise RoomError('Acceptance-review room, project or workflow identity changed')
    original_requests, current_requests = baseline.get('requests'), state.get('requests')
    if not isinstance(original_requests, dict) or not isinstance(current_requests, dict):
        raise RoomError('Acceptance-review retained request evidence is malformed')
    for request_id, original in original_requests.items():
        current = current_requests.get(request_id)
        if (not isinstance(original, dict) or not isinstance(current, dict)
                or any(current.get(key) != value for key, value in original.items() if key not in OBSERVATIONS)):
            raise RoomError('A prior request, receipt, verdict or counter changed since the acceptance-review audit')
    for key in ('acceptances', 'verifications'):
        original, current = baseline.get(key), state.get(key)
        if not isinstance(original, list) or not isinstance(current, list) or current[:len(original)] != original:
            raise RoomError('Acceptance-review retained acceptance or verification history changed')
    prefix = (evidence.get('journal') or {}).get('entries')
    if not isinstance(prefix, list) or entries[:len(prefix)] != prefix:
        raise RoomError('The acceptance-review projected journal no longer keeps its audited prefix')
    manifest = evidence.get('manifest')
    if not isinstance(manifest, dict):
        raise RoomError('The acceptance-review manifest is malformed')
    for path, expected in manifest.items():
        if ao.digest(ao_delegates.owned_bytes(_relative(directory, path), MAX_RECORD_BYTES)) != expected:
            raise RoomError('Acceptance-review retained evidence is missing or modified')
    if not unused:
        return
    for key in UNUSED_PINNED:
        if state.get(key) != baseline.get(key):
            raise RoomError('The unused acceptance-review grant pins ' + key
                            + '; it changed, so nothing may be dispatched until it is diagnosed')
    if set(current_requests) != set(original_requests):
        raise RoomError('The request identity set changed while an acceptance-review grant is unused')
    for key in ('acceptances', 'verifications'):
        if state.get(key) != baseline.get(key):
            raise RoomError('Acceptance-review acceptance or verification history changed while its grant is unused')


def _verified(directory, state, names, allow_pending):
    entries, digests, pending = _journal(directory, state, names, allow_pending)
    grants = [(index, entry) for index, entry in enumerate(entries) if entry['kind'] == 'grant']
    consumptions = [(index, entry) for index, entry in enumerate(entries) if entry['kind'] == 'consumption']
    grant_ids = [entry['inputs']['request_id'] for _, entry in grants]
    review_ids = [entry['grant']['review_request_id'] for _, entry in grants]
    if pending is not None:
        grant_ids.append(pending['inputs']['request_id'])
        review_ids.append(pending['grant']['review_request_id'])
    if (len(set(grant_ids)) != len(grant_ids) or len(set(review_ids)) != len(review_ids)
            or set(grant_ids) & set(review_ids)):
        raise RoomError('Acceptance-review grant and intended review identities were duplicated or repurposed')
    requests = state.get('requests') or {}
    if any(grant_id in requests for grant_id in grant_ids):
        raise RoomError('An acceptance-review grant request ID was repurposed as a room request')
    for number, (index, entry) in enumerate(grants, 1):
        if entry['retained_reviewer_attempts'] != LIMIT + number - 1:
            raise RoomError('An acceptance-review grant records the wrong retained reviewer attempt count')
    attempts = _reviewer_requests(state)
    if len(attempts) != LIMIT + len(consumptions):
        raise RoomError('Reviewer attempt accounting does not match the acceptance-review continuation journal')
    consumed = set()
    for number, (index, entry) in enumerate(consumptions, 1):
        request = attempts[LIMIT + number - 1]
        previous = entries[index - 1]
        if (index < 1 or previous['kind'] != 'grant' or entry['grant_sha256'] != digests[index - 1]
                or entry['grant_request_id'] != previous['inputs']['request_id']
                or entry['review_request_id'] != previous['grant']['review_request_id']
                or entry['message_sha256'] != previous['grant']['message_sha256']):
            raise RoomError('A consumption entry does not match its immediately preceding acceptance-review grant')
        if (request.get('request_id') != entry['review_request_id']
                or request.get('acceptance_review_consumption') != _entry_path(entry['sequence'])
                or request.get('acceptance_review_consumption_sha256') != digests[index]
                or request.get('acceptance_review_grant') != {'request_id': entry['grant_request_id'],
                                                              'sha256': entry['grant_sha256']}
                or any(request.get(key) != value for key, value in entry['request'].items() if key != 'state')):
            raise RoomError('The consumed acceptance review does not match its immutable consumption evidence')
        consumed.add(entry['review_request_id'])
    if {request.get('request_id') for request in requests.values() if _linkage(request)} != consumed:
        raise RoomError('A continuation linkage on a room request is forged, detached or modified; every linkage '
                        'must belong to exactly one consumption entry')
    if entries and entries[-1]['kind'] == 'grant' and entries[-1]['grant']['review_request_id'] in requests:
        raise RoomError('An unused acceptance-review grant intends a review request ID that already exists in this room')
    if pending is not None and pending['grant']['review_request_id'] in requests:
        raise RoomError('An uncommitted acceptance-review grant intends a review request ID that already exists in this room')
    latest = pending if pending is not None else (grants[-1][1] if grants else None)
    if latest is not None:
        evidence = _audit(directory, latest['inputs']['audit_sha256']).get('evidence')
        if (not isinstance(evidence, dict) or latest['evidence_sha256'] != ao.digest(evidence)
                or latest['before_state_sha256'] != evidence.get('state_sha256')
                or evidence.get('state_sha256') != ao.digest(evidence.get('state'))
                or not _grant_matches_target(latest['grant'], evidence.get('target'))):
            raise RoomError('The acceptance-review audit chain changed')
        unused = pending is not None or (bool(entries) and entries[-1]['kind'] == 'grant')
        _retained(directory, state, entries, evidence, unused)
    unconsumed = entries[-1] if entries and entries[-1]['kind'] == 'grant' else None
    return {'entries': entries, 'digests': digests, 'unconsumed': unconsumed,
            'unconsumed_sha256': digests[-1] if unconsumed is not None else None,
            'pending': pending, 'consumptions': len(consumptions)}


def validate(service, state, allow_pending=False):
    """Offline integrity and accounting only; never performs an AO call or a save."""
    try:
        directory = service.root / 'rooms' / state['room_id']
        reference = state.get(KEY)
        names = _names(directory)
        linked, reviewers = False, 0
        for request in (state.get('requests') or {}).values():
            if _linkage(request):
                linked = True
            if request.get('role') == 'reviewer':
                reviewers += 1
        if reference is None and not names and not linked:
            if not ao_workflow.normal(state) or reviewers <= LIMIT:
                return None
            raise RoomError('More than three reviewer-role requests exist without an acceptance-review continuation '
                            'journal; the room refuses mutation')
        if not ao_workflow.normal(state):
            raise RoomError('The acceptance-review continuation lane is only available for a normal Fable engineering room')
        return _verified(directory, state, names, allow_pending)
    except RoomError:
        raise
    except (KeyError, TypeError, ValueError, OSError, AttributeError, IndexError, RecursionError) as exc:
        raise RoomError('Acceptance-review continuation evidence is unreadable or inconsistent') from exc


def _owner(state, role, database, native_session_id, workspace):
    binding = (state.get('bindings') or {}).get(role) or {}
    try:
        if role == 'engineer':
            owner = ao_native_identity.read_owner(database, binding['session_id'])
        else:
            owner = ao_native_identity.read_codex_owner(database, binding['session_id'])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RoomError('The retained native ' + role + ' owner evidence is unavailable or contradictory') from exc
    if (owner.get('project_id') != state.get('ao_project_id')
            or owner.get('ao_conversation_id') != binding.get('conversation_id')
            or owner.get('active_branch_id') != binding.get('branch_id')
            or owner.get('provider_conversation_id') != native_session_id
            or owner.get('workspace_path') != str(workspace)):
        raise RoomError('The native ' + role + ' owner differs from the retained room, session, workspace or '
                        'supplied native session')
    if role == 'engineer':
        source = state.get('native_outcome_source')
        if (source and (source.get('database') != database or source.get('native_session_id') != native_session_id
                        or source.get('session_id') != binding.get('session_id'))):
            raise RoomError('Retained native source evidence contradicts the supplied owner database or session')
    # Runtime liveness/generation are observations, not replacement-provider authority.
    return {key: value for key, value in owner.items() if key not in ('activity_state', 'controller_generation')}


def _retained_owners(directory, entries):
    """The native ownership frozen by every earlier committed grant's own audit, or None before the
    first grant. A fresh audit compares its current owners against this value; it never redefines it."""
    common = None
    for entry in entries:
        if entry.get('kind') != 'grant':
            continue
        evidence = _audit(directory, entry['inputs']['audit_sha256']).get('evidence')
        native_owner = evidence.get('native_owner') if isinstance(evidence, dict) else None
        if (not isinstance(native_owner, dict) or set(native_owner) != {'engineer', 'reviewer'}
                or not isinstance(native_owner.get('engineer'), dict)
                or not isinstance(native_owner.get('reviewer'), dict)):
            raise RoomError('Acceptance-review continuation evidence is unreadable or inconsistent')
        if common is None:
            common = native_owner
        elif common != native_owner:
            raise RoomError('Earlier acceptance-review grants retain contradictory native ownership; diagnose '
                            'the chain before continuing')
    return common


def _manifest(directory, state, entries):
    """Every retained immutable record, including earlier failures, verdicts and grants."""
    paths = set()
    for key in ('spec', 'handoff', 'checkpoint'):
        if state.get(key):
            paths.add(state[key])
    paths.update(str(item.relative_to(directory)) for item in (directory / 'specs').glob('*.json'))
    for request in (state.get('requests') or {}).values():
        for key in ('receipt', 'semantic_outcome', 'outcome_resume', 'response_normalization',
                    'completion_candidate', 'engineering_record', 'reroute_evidence'):
            if request.get(key):
                paths.add(request[key])
        paths.update(request.get('receipt_history') or [])
        paths.update(request.get('reroute_history') or [])
        request_id = ao.identifier(request['request_id'])
        for folder in ('receipts', 'outcomes', 'outcome-resumes'):
            paths.update(str(item.relative_to(directory))
                         for item in (directory / folder / request_id).glob('*.json'))
    for verification in state.get('verifications') or []:
        if not verification.get('path'):
            continue
        paths.add(verification['path'])
        checkpoint = _load(_relative(directory, verification['path']))
        for gate in checkpoint['gates']:
            log = _relative(directory, gate['log'])
            if ao.digest(ao_delegates.owned_bytes(log, MAX_RECORD_BYTES)) != gate['log_sha256']:
                raise RoomError('Retained verification evidence was modified')
            paths.add(gate['log'])
    for entry in entries:
        paths.add(_entry_path(entry['sequence']))
        if entry['kind'] == 'grant':
            paths.add(BASE + '/audits/' + entry['inputs']['audit_sha256'] + '.json')
    result = {}
    for path in sorted(paths):
        result[path] = ao.digest(ao_delegates.owned_bytes(_relative(directory, path), MAX_RECORD_BYTES))
    return result


def _inspect(service, directory, state, target, phase):
    """Shared eligibility and evidence; the evidence dict is always built last."""
    if phase != 'admission':
        service.settled(state, pending_acceptance_extension=(phase == 'reconcile'))
    if not ao_workflow.normal(state):
        raise RoomError('An additional acceptance review is only available for a normal Fable engineering room')
    bindings = state.get('bindings') or {}
    engineer, reviewer = bindings.get('engineer'), bindings.get('reviewer')
    if (not engineer or engineer.get('harness') != 'claude-code' or engineer.get('model') != ao_workflow.FABLE_MODEL
            or engineer.get('reasoning_effort') != 'max' or not reviewer or reviewer.get('harness') != 'codex'
            or reviewer.get('reasoning_effort') != 'max'):
        raise RoomError('An additional acceptance review requires the bound native Fable engineer and Codex '
                        'reviewer at max effort')
    for request in (state.get('requests') or {}).values():
        if request.get('role') == 'reviewer' and any(request.get(key) != reviewer.get(key)
                                                    for key in ('session_id', 'model', 'reasoning_effort', 'harness')):
            raise RoomError('Every retained reviewer attempt must belong to the currently bound native Codex '
                            'reviewer; the reviewer cannot be replaced')
    committed = validate(service, state, allow_pending=(phase == 'reconcile'))
    unconsumed = committed['unconsumed'] if committed else None
    pending = committed['pending'] if committed else None
    consumptions = committed['consumptions'] if committed else 0
    attempts = _reviewer_requests(state)
    if len(attempts) < LIMIT:
        raise RoomError('No additional acceptance review can be granted before the ordinary three reviewer '
                        'attempts are exhausted')
    if len(attempts) != LIMIT + consumptions:
        raise RoomError('Reviewer attempt accounting does not match the acceptance-review continuation journal')
    if phase in ('audit', 'extend'):
        if unconsumed is not None:
            raise RoomError('An unused acceptance-review grant already exists; only its one named review may be '
                            'dispatched, and it cannot be replaced')
        if pending is not None:
            raise RoomError('An uncommitted acceptance-review grant receipt exists; reconcile only its identical request')
        if target['review_request_id'] in (state.get('requests') or {}):
            raise RoomError('The intended reviewer request ID is already used in this room')
        for entry in (committed['entries'] if committed else []):
            if entry['kind'] == 'grant' and target['review_request_id'] in (entry['inputs']['request_id'],
                                                                           entry['grant']['review_request_id']):
                raise RoomError('The intended reviewer request ID already belongs to an acceptance-review grant '
                                'and may not be repurposed')
    elif phase == 'reconcile':
        if pending is None:
            raise RoomError('No uncommitted acceptance-review grant receipt is available to reconcile')
        if (target['review_request_id'] != pending['grant']['review_request_id']
                or target['message_sha256'] != pending['grant']['message_sha256']
                or target['purpose'] != pending['grant']['purpose']):
            raise RoomError('The uncommitted acceptance-review grant belongs to another payload; only its '
                            'identical request may reconcile it')
        if unconsumed is not None:
            raise RoomError('An unused acceptance-review grant already exists; only its one named review may be '
                            'dispatched, and it cannot be replaced')
    elif phase == 'admission':
        if unconsumed is None:
            raise RoomError(EXHAUSTED if committed is None else
                            'Every audited acceptance-review grant is consumed; a further additional review '
                            'needs a fresh audit and grant')
        if target['review_request_id'] != unconsumed['grant']['review_request_id']:
            raise RoomError('Only the exact reviewer request identity named by the unused grant may be dispatched')
        if unconsumed['grant']['review_request_id'] in (state.get('requests') or {}):
            raise RoomError('The unused acceptance-review grant intends a review request ID that already exists '
                            'in this room; diagnose without resending')
    else:
        raise RoomError('Unknown acceptance-review continuation phase')
    spec = service.spec(directory, state)
    checkpoint = service.checkpoint(directory, state)
    engineering_request = ao_workflow.engineering_ready(service, directory, state)
    ao_workflow.engineering_report(directory, state, engineering_request)
    handoff = ao_workflow.handoff_record(directory, state)
    actual = ao_workflow.workspace(service, directory, state, check_routing=False)
    if checkpoint['candidate_path'] != str(actual) or str(actual) != handoff['worktree']:
        raise RoomError('The passed checkpoint, engineer workspace and handoff must name the same candidate')
    if checkpoint['candidate_sha256'] != engineering_request['result_candidate_sha256']:
        raise RoomError('The passed checkpoint does not match the completed engineering candidate')
    if state.get('executable_binding') or list((directory / 'executable-bindings').glob('*.json')):
        import ao_executable_binding
        ao_executable_binding._chain(directory, state, _load(_relative(directory, state['preparation'])))
    import ao_review_extension
    from ao_provider_transition import _CompleteClient, _ReadOnlyIdentity
    client = _CompleteClient(service.client(state))
    observer = _ReadOnlyIdentity(service)
    snapshots = {}
    for role in ('engineer', 'reviewer'):
        snapshots[role] = observer.identity(client, state, bindings[role])
    for snapshot in snapshots.values():
        if ao.busy(snapshot):
            raise RoomError('A bound AO conversation has active work; the acceptance-review continuation '
                            'refuses before any write')
    try:
        native = {role: ao_review_extension._native_evidence(directory, state, bindings[role], snapshots[role])
                  for role in ('engineer', 'reviewer')}
    except RoomError:
        raise
    except (KeyError, TypeError, ValueError, OSError, AttributeError, IndexError) as exc:
        raise RoomError('Native acceptance-review evidence is unreadable or incomplete') from exc
    import ao_outcomes
    ao_outcomes.gate(service, directory, state, 'reviewer', target['review_request_id'], snapshots['reviewer'])
    from ao_reviewer_recovery import _workspace
    reviewer_workspace = _workspace(service, state, reviewer['session_id'], str(actual))
    owners = {'engineer': _owner(state, 'engineer', target['native_owner_database'],
                                 target['engineer_native_session_id'], str(actual)),
              'reviewer': _owner(state, 'reviewer', target['native_owner_database'],
                                 target['reviewer_native_session_id'], reviewer_workspace)}
    # A fresh audit may re-observe the same native identity (including after a positively
    # verified same-native controller restart, since _owner already excludes activity_state and
    # controller_generation); it must never redefine either role's identity frozen by an earlier
    # grant in this room's continuation chain.
    retained = _retained_owners(directory, committed['entries'] if committed else [])
    if retained is not None:
        for role in ('engineer', 'reviewer'):
            if owners[role] != retained[role]:
                raise RoomError('The native ' + role + ' owner differs from the ownership retained by earlier '
                                'acceptance-review grants; a fresh audit cannot redefine the retained native '
                                + role)
    engineering_record = _load(_relative(directory, engineering_request['engineering_record']))
    reviewer_requests = _reviewer_requests(state)
    journal_entries = committed['entries'] if committed else []
    evidence = {
        'room': {key: state.get(key) for key in ('room_id', 'project_path', 'git_common_dir', 'ao_project_id',
                                                 'ao_url', 'workflow', 'bindings')},
        'target': dict(target),
        'spec': {'revision': spec['revision'], 'sha256': spec['sha256'],
                 'spec_record_sha256': state['spec_record_sha256'], 'gates': spec['gates']},
        'checkpoint': {'path': state['checkpoint'], 'sha256': state['checkpoint_sha256'],
                       'id': checkpoint.get('id'), 'passed': checkpoint.get('passed'),
                       'spec_sha256': checkpoint['spec_sha256'],
                       'spec_record_sha256': checkpoint['spec_record_sha256'],
                       'candidate_sha256': checkpoint['candidate_sha256'],
                       'candidate_path': checkpoint['candidate_path'],
                       'gates': [{key: gate.get(key) for key in ('argv', 'return_code', 'timed_out', 'log',
                                                                 'log_sha256')}
                                 for gate in checkpoint['gates']]},
        'engineering': {'request_id': engineering_request['request_id'],
                        'purpose': engineering_request.get('purpose'),
                        'handoff_sha256': engineering_request.get('handoff_sha256'),
                        'receipt_sha256': engineering_request['receipt_sha256'],
                        'report_sha256': engineering_record['report_sha256'],
                        'engineering_record': engineering_request['engineering_record'],
                        'engineering_record_sha256': engineering_request['engineering_record_sha256'],
                        'result_candidate_sha256': engineering_request['result_candidate_sha256'],
                        'delegation': engineering_record.get('delegation'),
                        'delegate_job_ids': engineering_request.get('delegate_job_ids')},
        'reviews': [{key: request.get(key) for key in ('request_id', 'created_order', 'session_id', 'model',
                                                       'reasoning_effort', 'harness', 'text_sha256',
                                                       'client_message_id', 'spec_record_sha256', 'review',
                                                       'turn_id', 'provider_turn_id', 'receipt', 'receipt_sha256',
                                                       'response_normalization_sha256',
                                                       'acceptance_review_consumption_sha256')}
                    for request in reviewer_requests],
        'retained_reviewer_attempts': len(reviewer_requests),
        'history': {'acceptances': len(state.get('acceptances') or []),
                    'acceptances_sha256': ao.digest(state.get('acceptances') or []),
                    'verifications': len(state.get('verifications') or []),
                    'verifications_sha256': ao.digest(state.get('verifications') or [])},
        'journal': {'entries': copy.deepcopy(journal_entries),
                    'head_sha256': (committed['digests'][-1] if committed and committed['digests'] else None),
                    'grants': sum(1 for entry in journal_entries if entry['kind'] == 'grant'),
                    'consumptions': sum(1 for entry in journal_entries if entry['kind'] == 'consumption')},
        'native': native,
        'native_owner': owners,
        'workspaces': {'engineer': str(actual), 'reviewer': reviewer_workspace},
        'manifest': _manifest(directory, state, journal_entries),
        'state_sha256': ao.digest(state),
        'state': copy.deepcopy(state),
    }
    return evidence


def audit(service, room_id, review_request_id, message_sha256, native_owner_database,
          engineer_native_session_id, reviewer_native_session_id):
    ao.identifier(review_request_id)
    _hash(message_sha256, 'intended reviewer message')
    ao.nonempty(native_owner_database, 'native_owner_database')
    ao.nonempty(engineer_native_session_id, 'engineer_native_session_id', 160)
    ao.nonempty(reviewer_native_session_id, 'reviewer_native_session_id', 160)
    target = {'review_request_id': review_request_id, 'message_sha256': message_sha256, 'purpose': PURPOSE,
              'native_owner_database': native_owner_database,
              'engineer_native_session_id': engineer_native_session_id,
              'reviewer_native_session_id': reviewer_native_session_id}
    with service.locked(room_id) as (directory, state):
        evidence = _inspect(service, directory, state, target, 'audit')
        value = {'version': 1, 'evidence': evidence, 'observed_at': time.time()}
        audit_sha256 = ao.digest(value)
        _roots(directory)
        _store(directory / BASE / 'audits' / (audit_sha256 + '.json'), value, 'complete acceptance-review audit')
        return {'eligible': True, 'audit_sha256': audit_sha256, 'room_id': room_id,
                'review_request_id': target['review_request_id'], 'message_sha256': message_sha256,
                'purpose': PURPOSE, 'spec_revision': evidence['spec']['revision'],
                'spec_sha256': evidence['spec']['sha256'],
                'spec_record_sha256': evidence['spec']['spec_record_sha256'],
                'candidate_sha256': evidence['checkpoint']['candidate_sha256'],
                'checkpoint': evidence['checkpoint']['path'],
                'evidence_sha256': evidence['checkpoint']['sha256'],
                'engineering_request_id': evidence['engineering']['request_id'],
                'engineer_session_id': state['bindings']['engineer']['session_id'],
                'reviewer_session_id': state['bindings']['reviewer']['session_id'],
                'retained_reviewer_attempts': evidence['retained_reviewer_attempts'],
                'prior_grants': evidence['journal']['grants'],
                'additional_acceptance_reviews': 1, 'model_dispatch': False,
                'state_writes': 'Saves private audit evidence and may persist ordinary outcome observations; '
                                'no dispatch, lifecycle operation, candidate edit or grant'}


def _result(entries, digests, index):
    entry = entries[index]
    following = entries[index + 1] if index + 1 < len(entries) else None
    return {'extended': True, 'request_id': entry['inputs']['request_id'],
            'review_request_id': entry['grant']['review_request_id'],
            'message_sha256': entry['grant']['message_sha256'], 'purpose': PURPOSE,
            'additional_acceptance_reviews': 1,
            'remaining_additional_reviews': 1 if index == len(entries) - 1 else 0,
            'consumed_by': (following['review_request_id'] if following is not None
                            and following['kind'] == 'consumption' else None),
            'audit_sha256': entry['inputs']['audit_sha256'], 'sequence': entry['sequence'],
            'receipt': _entry_path(entry['sequence']), 'receipt_sha256': digests[index],
            'model_dispatch': False,
            'meaning': 'Exactly one named acceptance review; not a raised counter. Holds, spec-review allowances '
                       'and all ordinary dispatch and acceptance checks stay unchanged'}


def extend(service, room_id, audit_sha256, request_id, authorization, authorization_reference, diagnosis):
    ao.identifier(request_id)
    _hash(audit_sha256, 'acceptance-review audit')
    ao.nonempty(authorization, 'authorization: actual user authorization and its approval context')
    ao.nonempty(authorization_reference, 'authorization_reference: source and provenance reference', 2048)
    ao.nonempty(diagnosis, 'diagnosis')
    inputs = {'request_id': request_id, 'audit_sha256': audit_sha256, 'authorization': authorization,
              'authorization_reference': authorization_reference, 'diagnosis': diagnosis}
    with service.locked(room_id) as (directory, state):
        committed = validate(service, state, allow_pending=True)
        entries = committed['entries'] if committed else []
        digests = committed['digests'] if committed else []
        pending = committed['pending'] if committed else None
        for index, entry in enumerate(entries):
            if entry['kind'] == 'grant' and entry['inputs']['request_id'] == request_id:
                if entry['inputs'] != inputs:
                    raise RoomError('That acceptance-review grant request ID already belongs to another payload; '
                                    'it cannot be renewed or renumbered')
                return _result(entries, digests, index)
        if pending is not None and pending['inputs'] != inputs:
            raise RoomError('The uncommitted acceptance-review grant belongs to another payload; only its '
                            'identical request may reconcile it')
        saved = _audit(directory, audit_sha256)
        target = saved.get('evidence', {}).get('target')
        if not isinstance(target, dict):
            raise RoomError('The acceptance-review audit has no usable target')
        if (request_id in (state.get('requests') or {})
                or request_id == target.get('review_request_id')
                or any(entry['inputs']['request_id'] == request_id
                       or entry['grant']['review_request_id'] == request_id for entry in entries if entry['kind'] == 'grant')):
            raise RoomError('The acceptance-review grant request ID must be unused and may not repurpose a '
                            'review identity')
        evidence = _inspect(service, directory, state, target, 'reconcile' if pending is not None else 'extend')
        if evidence != saved.get('evidence'):
            raise RoomError('The acceptance-review audit is stale; room, candidate, native or retained evidence changed')
        sequence = len(entries) + 1
        entry = {'version': 1, 'room_id': state['room_id'], 'sequence': sequence,
                 'previous_sha256': digests[-1] if digests else None, 'kind': 'grant', 'inputs': inputs,
                 'grant': {'review_request_id': target['review_request_id'],
                           'message_sha256': target['message_sha256'], 'purpose': PURPOSE,
                           'role': 'reviewer', 'additional_acceptance_reviews': 1},
                 'retained_reviewer_attempts': evidence['retained_reviewer_attempts'],
                 'before_state_sha256': evidence['state_sha256'],
                 'evidence_sha256': ao.digest(evidence),
                 'recorded_at': (pending['recorded_at'] if pending is not None else time.time())}
        if pending is not None and pending != entry:
            raise RoomError('The uncommitted acceptance-review grant differs from the recomputed grant; its bytes '
                            'are preserved for diagnosis')
        repeated = _inspect(service, directory, state, target, 'reconcile' if pending is not None else 'extend')
        if repeated != evidence:
            raise RoomError('Acceptance-review continuation evidence changed before commit; nothing was stored')
        relative = _entry_path(sequence)
        if pending is None:
            _roots(directory)
            _store(directory / relative, entry, 'acceptance-review continuation journal entry')
            digest_value = ao.digest(entry)
        else:
            digest_value = ao.digest(pending)
        reference = state.get(KEY)
        if not isinstance(reference, dict):
            reference = {'version': 1, 'entries': []}
            state[KEY] = reference
        reference['entries'].append({'sequence': sequence, 'kind': 'grant', 'path': relative,
                                     'sha256': digest_value})
        service.save(directory, state)
        grown = entries + [entry]
        return _result(grown, digests + [digest_value], len(grown) - 1)


def guard_unused(service, state, operation):
    committed = validate(service, state)
    if committed is None or committed['unconsumed'] is None:
        return
    raise RoomError('An unused acceptance-review grant freezes this room until its one named review is '
                    'dispatched; ' + str(operation) + ' was refused before any write')


def guard_outcome_resume(service, state, request, resume_request_id):
    committed = validate(service, state)
    if committed is None or committed['unconsumed'] is None:
        return
    if (not isinstance(request, dict) or request.get('role') != 'reviewer'
            or resume_request_id != committed['unconsumed']['grant']['review_request_id']):
        raise RoomError('An unused acceptance-review grant freezes this room until its one named review is '
                        'dispatched; this outcome continuation was refused before any write')


def before_send(service, state, role, request_id):
    committed = validate(service, state)
    if committed is None:
        return
    for entry in committed['entries']:
        if entry['kind'] == 'grant' and entry['inputs']['request_id'] == request_id:
            raise RoomError('That request ID is an acceptance-review grant identity and may not be repurposed '
                            'for a native dispatch')
    if committed['unconsumed'] is not None and role != 'reviewer':
        raise RoomError('An unused acceptance-review grant freezes this room until its one named review is '
                        'dispatched; ' + str(role) + ' dispatch was refused before any write')


def admission(service, directory, state, request_id, message, purpose):
    if state.get(KEY) is None and not _names(directory) and not any(
            _linkage(request) for request in (state.get('requests') or {}).values()):
        raise RoomError(EXHAUSTED)
    committed = validate(service, state)
    if committed is None:
        raise RoomError(EXHAUSTED)
    grant = committed['unconsumed']
    if grant is None:
        raise RoomError('Every audited acceptance-review grant is consumed; a further additional review needs '
                        'a fresh audit and grant')
    if purpose not in (None, PURPOSE):
        raise RoomError('An additional acceptance review must use the acceptance_review purpose')
    if request_id != grant['grant']['review_request_id']:
        raise RoomError('Only the exact reviewer request identity named by the unused grant may be dispatched')
    if not isinstance(message, str):
        raise RoomError('The dispatch message must be the exact caller message string named by the grant')
    if ao.digest(message.encode()) != grant['grant']['message_sha256']:
        raise RoomError('The dispatch message does not match the exact audited caller-message digest for this grant')
    evidence = _audit(directory, grant['inputs']['audit_sha256']).get('evidence')
    if not isinstance(evidence, dict) or not isinstance(evidence.get('target'), dict):
        raise RoomError('The acceptance-review audit chain changed')
    current = _inspect(service, directory, state, evidence['target'], 'admission')
    for key in LIVE:
        if current.get(key) != evidence.get(key):
            raise RoomError('The acceptance-review grant is stale: the current ' + key + ' evidence differs '
                            'from its audit, so nothing was consumed')
    return {'request_id': grant['inputs']['request_id'], 'sha256': committed['unconsumed_sha256'],
            'sequence': grant['sequence'], 'review_request_id': grant['grant']['review_request_id'],
            'message_sha256': grant['grant']['message_sha256'], 'audit_sha256': grant['inputs']['audit_sha256']}


def consume(service, directory, state, request, grant):
    """Store the immutable send intent before the state projection and before any POST."""
    committed = validate(service, state)
    if committed is None or committed['unconsumed'] is None:
        raise RoomError('An unused exact acceptance-review grant is required before its named review can be dispatched')
    unconsumed = committed['unconsumed']
    if (not isinstance(grant, dict) or grant.get('sha256') != committed['unconsumed_sha256']
            or grant.get('sequence') != unconsumed['sequence']
            or grant.get('review_request_id') != unconsumed['grant']['review_request_id']):
        raise RoomError('The acceptance-review grant changed before its named review could be consumed; '
                        'nothing was written')
    evidence = _audit(directory, unconsumed['inputs']['audit_sha256']).get('evidence')
    if not isinstance(evidence, dict):
        raise RoomError('The acceptance-review audit chain changed')
    reviewer = (state.get('bindings') or {}).get('reviewer') or {}
    expected_review = {'spec_sha256': evidence['spec']['sha256'],
                       'candidate_sha256': evidence['checkpoint']['candidate_sha256'],
                       'evidence_sha256': evidence['checkpoint']['sha256']}
    if (request.get('request_id') != unconsumed['grant']['review_request_id']
            or request.get('request_id') in (state.get('requests') or {})
            or request.get('role') != 'reviewer' or request.get('purpose') != PURPOSE
            or request.get('state') != 'uncertain'
            or not isinstance(request.get('client_message_id'), str) or not request['client_message_id'].strip()
            or not _is_hex(request.get('text_sha256'))
            or any(request.get(key) != value for key, value in reviewer.items())
            or request.get('spec_record_sha256') != evidence['spec']['spec_record_sha256']
            or request.get('review') != expected_review):
        raise RoomError('Only the exact new acceptance-review intent named by the unused grant may consume it')
    request['acceptance_review_grant'] = {'request_id': unconsumed['inputs']['request_id'],
                                          'sha256': committed['unconsumed_sha256']}
    entry = {'version': 1, 'room_id': state['room_id'], 'sequence': unconsumed['sequence'] + 1,
             'previous_sha256': committed['unconsumed_sha256'], 'kind': 'consumption',
             'grant_sha256': committed['unconsumed_sha256'],
             'grant_request_id': unconsumed['inputs']['request_id'],
             'review_request_id': unconsumed['grant']['review_request_id'],
             'message_sha256': unconsumed['grant']['message_sha256'],
             'client_message_id': request['client_message_id'],
             'text_sha256': request['text_sha256'],
             'request': copy.deepcopy(request),
             'recorded_at': time.time()}
    relative = _entry_path(entry['sequence'])
    _roots(directory)
    _store(directory / relative, entry, 'acceptance-review continuation journal entry')
    digest_value = ao.digest(entry)
    state[KEY]['entries'].append({'sequence': entry['sequence'], 'kind': 'consumption',
                                  'path': relative, 'sha256': digest_value})
    request['acceptance_review_consumption'] = relative
    request['acceptance_review_consumption_sha256'] = digest_value


def summary(service, state):
    """Read-only status block; never raises and never certifies corrupted evidence as valid."""
    try:
        committed = validate(service, state, allow_pending=True)
        if committed is None:
            return None
        pending = committed['pending']
        if pending is not None:
            return {'state': 'pending_uncommitted', 'request_id': pending['inputs']['request_id'],
                    'review_request_id': pending['grant']['review_request_id'], 'model_dispatch': False,
                    'meaning': 'A durable acceptance-review grant receipt exists without its state projection; '
                               'only the identical extend request may reconcile it and no review may be dispatched'}
        unconsumed = committed['unconsumed']
        entries = committed['entries']
        consumptions = [entry for entry in entries if entry['kind'] == 'consumption']
        return {'state': 'unconsumed' if unconsumed is not None else 'consumed',
                'grants': sum(1 for entry in entries if entry['kind'] == 'grant'),
                'consumed': len(consumptions),
                'retained_reviewer_attempts': len(_reviewer_requests(state)),
                'remaining_additional_reviews': 1 if unconsumed is not None else 0,
                'unconsumed_grant': ({'request_id': unconsumed['inputs']['request_id'],
                                      'review_request_id': unconsumed['grant']['review_request_id'],
                                      'message_sha256': unconsumed['grant']['message_sha256'],
                                      'purpose': unconsumed['grant']['purpose'],
                                      'audit_sha256': unconsumed['inputs']['audit_sha256'],
                                      'receipt': _entry_path(unconsumed['sequence']),
                                      'receipt_sha256': committed['unconsumed_sha256']}
                                     if unconsumed is not None else None),
                'consumed_by': [entry['review_request_id'] for entry in consumptions],
                'model_dispatch': False,
                'meaning': 'Exactly one audited additional acceptance review per grant; never a raised counter. '
                           'Every ordinary dispatch, hold, verification and acceptance check still applies'}
    except (RoomError, OSError, ValueError, KeyError, TypeError, AttributeError, IndexError, RecursionError) as exc:
        return {'state': 'inconsistent', 'error': str(exc)[:500], 'model_dispatch': False,
                'meaning': 'Acceptance-review continuation evidence failed verification; every mutation refuses '
                           'until it is diagnosed, and this status never certifies corrupted evidence as valid'}
