"""Bounded local Claude error evidence. Reads no account settings or credentials."""
import array
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid
import xml.etree.ElementTree as ET

from room import RoomError


def timestamp(value):
    """Parse a native timestamp; malformed rows refuse as RoomError."""
    if not isinstance(value, str):
        raise RoomError('Native event timestamp is malformed')
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except ValueError as exc:
        raise RoomError('Native event timestamp is malformed') from exc


def human_text(row):
    if (row.get('type') != 'user' or row.get('isCompactSummary')
            or not isinstance(row.get('origin'), dict) or row['origin'].get('kind') != 'human'):
        return None
    message = row.get('message')
    content = message.get('content') if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if (not isinstance(content, list) or not content
            or any(not isinstance(x, dict) or x.get('type') != 'text' or not isinstance(x.get('text'), str) for x in content)):
        return None
    return ''.join(x['text'] for x in content)


COMPACTION_THRASHING_TEXT = (
    'Autocompact is thrashing: the context refilled to the limit within 3 turns of the previous compact, '
    '3 times in a row. A file being read or a tool output is likely too large for the context window. '
    'Try reading in smaller chunks, or use /clear to start fresh.'
)
COMPACTION_THRASHING_TEXT_SHA256 = hashlib.sha256(COMPACTION_THRASHING_TEXT.encode()).hexdigest()


def _message_text(message):
    if not isinstance(message, dict):
        return None
    content = message.get('content')
    if isinstance(content, str):
        return content
    if (isinstance(content, list) and content and all(isinstance(x, dict)
            and x.get('type') == 'text' and isinstance(x.get('text'), str) for x in content)):
        return ''.join(x['text'] for x in content)
    return None


def _assistant_substantive(message):
    '''Only demonstrably empty supported text content is non-substantive.

    Unknown or opaque non-text blocks and malformed text are ambiguous and
    therefore disqualify this exact lane.
    '''
    if not isinstance(message, dict):
        return True
    content = message.get('content')
    if isinstance(content, str):
        return bool(content.strip())
    if not isinstance(content, list):
        return True
    for block in content:
        if not isinstance(block, dict) or block.get('type') != 'text':
            return True
        text = block.get('text')
        if not isinstance(text, str) or text.strip():
            return True
    return False


def _non_substantive_continuation(message):
    """Only an absent, empty or exact-error text body can continue the exact error."""
    if not isinstance(message, dict):
        return False
    content = message.get('content')
    if content is None:
        return True
    if isinstance(content, str):
        return content in ('', COMPACTION_THRASHING_TEXT)
    if isinstance(content, list):
        if not content:
            return True
        if all(isinstance(block, dict) and block.get('type') == 'text'
               and isinstance(block.get('text'), str) for block in content):
            return ''.join(block['text'] for block in content) in ('', COMPACTION_THRASHING_TEXT)
    return False


def _api_error_nested(row):
    '''The dict form of apiError, or {} when the field is absent or a string code.

    Claude Code >=2.1.29x writes apiError as a string such as "usage_limit_reached" and
    moves the structured detail to apiErrorParams and quotaLimits; those fields are never
    read for classification — apiErrorStatus and error still carry the typed failure.
    Older versions used a nested dict or omitted the field entirely. The string->{} folding
    serves outcome classification only: envelope-equality and thrashing-proof sites reject a
    string apiError before reaching this helper.'''
    nested = row.get('apiError') or {}
    if isinstance(nested, str):
        return {}
    return nested


def _same_error_envelope(row):
    """Later same-id synthetic records must not contradict the exact typed error."""
    # A string apiError carries a 2.1.29x code we cannot equate with the legacy
    # dict envelope; envelope equality stays legacy-only until a qualified
    # 2.1.29x thrashing row exists.
    if isinstance(row.get('apiError'), str):
        return False
    nested = _api_error_nested(row)
    if not isinstance(nested, dict):
        return False
    if row.get('apiErrorStatus', nested.get('status')) is not None:
        return False
    if row.get('quotaLimits') not in (None, {}, []):
        return False
    if any(row.get(key) not in (None, False, '') for key in
           ('rateLimitType', 'quota', 'quota_limit', 'safety', 'refusal')):
        return False
    return row.get('error') in (None, 'invalid_request')


def contradictory_stop(value):
    """A refusal or safety stop is a different terminal outcome than the exact thrashing error."""
    return isinstance(value, str) and ('refusal' in value or 'safety' in value)


def compaction_failure(events, request, session_id, workspace, outcome):
    '''Return the narrow, positively bound autocompact-thrashing proof or None.'''
    from ao_project_room import digest
    if (not isinstance(events, list) or any(not isinstance(row, dict) for row in events)
            or not isinstance(request, dict) or not isinstance(session_id, str) or not session_id
            or not isinstance(workspace, str) or not workspace or not isinstance(outcome, dict)
            or outcome.get('next_human_uuid') is not None):
        return None
    anchor_uuid = outcome.get('anchor_uuid')
    errors = outcome.get('errors')
    if (not isinstance(anchor_uuid, str) or not anchor_uuid
            or not isinstance(errors, list) or len(errors) != 1
            or not isinstance(errors[0], dict) or errors[0].get('error') != 'invalid_request'):
        return None
    error_uuid = errors[0].get('uuid')
    if not isinstance(error_uuid, str) or not error_uuid or error_uuid == anchor_uuid:
        return None
    stops = outcome.get('stop_reasons')
    if not isinstance(stops, list) or any(contradictory_stop(stop) for stop in stops):
        return None
    anchor_index = error_index = None
    for index, row in enumerate(events):
        if row.get('sessionId') != session_id or row.get('isSidechain'):
            continue
        if row.get('agentId') is not None or row.get('agent_id') is not None:
            if row.get('uuid') in (anchor_uuid, error_uuid):
                return None
            continue
        if row.get('uuid') == anchor_uuid:
            if anchor_index is not None:
                return None
            anchor_index = index
        if row.get('uuid') == error_uuid:
            if error_index is not None:
                return None
            error_index = index
    if anchor_index is None or error_index is None or anchor_index >= error_index:
        return None
    anchor, error_row = events[anchor_index], events[error_index]
    if human_text(anchor) is None or anchor.get('cwd') != workspace or error_row.get('cwd') != workspace:
        return None
    anchor_message = anchor.get('message')
    if not isinstance(anchor_message, dict) or anchor_message.get('role') != 'user':
        return None
    if error_row.get('type') != 'assistant' or error_row.get('isApiErrorMessage') is not True:
        return None
    message = error_row.get('message')
    if not isinstance(message, dict) or message.get('role') != 'assistant':
        return None
    if message.get('model') != '<synthetic>' or message.get('stop_reason') != 'stop_sequence':
        return None
    if _message_text(message) != COMPACTION_THRASHING_TEXT:
        return None
    # Same rule as _same_error_envelope: a string apiError of any value makes the
    # row ineligible — the thrashing proof stays legacy-envelope-only until a
    # qualified 2.1.29x thrashing row exists.
    if isinstance(error_row.get('apiError'), str):
        return None
    nested = _api_error_nested(error_row)
    if not isinstance(nested, dict):
        return None
    if error_row.get('apiErrorStatus', nested.get('status')) is not None:
        return None
    if error_row.get('quotaLimits') not in (None, {}, []):
        return None
    if any(error_row.get(key) not in (None, False, '') for key in
           ('rateLimitType', 'quota', 'quota_limit', 'safety', 'refusal')):
        return None
    for projection in errors + (outcome.get('settled_errors') or []):
        if (not isinstance(projection, dict) or projection.get('error') != 'invalid_request'
                or projection.get('http_status') is not None):
            return None
    start = timestamp(anchor['timestamp'])
    for index, row in enumerate(events):
        if row.get('sessionId') != session_id or row.get('isSidechain'):
            continue
        try:
            row_time = timestamp(row.get('timestamp'))
        except (RoomError, KeyError, TypeError, ValueError, AttributeError):
            if human_text(row) is not None or row.get('type') == 'assistant':
                return None
            continue
        if (human_text(row) is not None and row.get('uuid') != anchor_uuid
                and (row_time >= start or index > anchor_index)):
            # A source-order-later human record is a new instruction even when
            # its timestamp is rewound or inconsistent; do not sort it away.
            return None
    error_start = timestamp(error_row['timestamp'])
    error_message_id = message.get('id') if isinstance(message.get('id'), str) and message['id'] else None
    for index, row in enumerate(events):
        if row.get('sessionId') != session_id or row.get('uuid') == error_uuid:
            continue
        if row.get('type') != 'assistant' or row.get('uuid') == anchor_uuid:
            continue
        later = row.get('message')
        if not isinstance(later, dict):
            return None
        try:
            row_time = timestamp(row.get('timestamp'))
        except (RoomError, KeyError, TypeError, ValueError, AttributeError):
            return None
        if not (index > error_index or (index > anchor_index and row_time >= error_start)):
            continue
        if contradictory_stop(later.get('stop_reason')):
            return None
        same_error = (error_message_id is not None and later.get('id') == error_message_id
                      and later.get('role') == 'assistant'
                      and later.get('model') == '<synthetic>'
                      and later.get('stop_reason') in (None, 'stop_sequence')
                      and _same_error_envelope(row))
        if same_error:
            if not _non_substantive_continuation(later):
                return None
            continue
        if row.get('isApiErrorMessage') is True or later.get('model') == '<synthetic>':
            return None
        if _assistant_substantive(later):
            return None
    return {
        'version': 1,
        'kind': 'autocompact_thrashing',
        'error': 'invalid_request',
        'error_role': 'assistant',
        'error_model': '<synthetic>',
        'error_stop_reason': 'stop_sequence',
        'error_text_sha256': COMPACTION_THRASHING_TEXT_SHA256,
        'error_message_id': error_message_id,
        'assistant_uuid': error_uuid,
        'anchor_uuid': anchor_uuid,
        'next_human_uuid': None,
        'later_substantive': False,
        'http_status_absent': True,
        'workspace_sha256': digest(workspace.encode()),
    }


def events_outcome(events, request, session_id, workspace=None):
    """Owned native outcome. An exact request keeps exact model equality and its historical result shape.

    A family engineer request (its frozen engineering_resolution names a family) accepts only exact
    identifiers of that family on its non-synthetic stop rows, equal to its frozen expectation when
    one exists, never more than one distinct model, and records them as observed_models.
    """
    from ao_project_room import digest
    from ao_engineering_model import _family_of, model_matches, qualification_of
    if (not isinstance(events, list) or any(not isinstance(x, dict) for x in events)
            or digest(request['text'].encode()) != request['text_sha256']):
        raise RoomError('Malformed native evidence or changed caller bytes')
    family = None if workspace is not None else _family_of(request)
    qualified = None if family is None else qualification_of(request)
    observed, stop_rows = set(), []
    rows = sorted((x for x in events if x.get('sessionId') == session_id and not x.get('isSidechain')
                   and x.get('type') in ('user', 'assistant') and x.get('timestamp')),
                  key=lambda x: timestamp(x['timestamp']))
    unique = {}
    for row in rows:
        identity = row.get('uuid')
        if not isinstance(identity, str) or not identity:
            raise RoomError('Native event lacks its identity')
        if identity in unique and unique[identity] != row:
            raise RoomError('Conflicting duplicate native event')
        unique[identity] = row
    rows = list(unique.values())
    anchors = [x for x in rows if (value := human_text(x)) is not None
               and request['created_at'] <= timestamp(x['timestamp']) <= request['created_at'] + 60
               and digest(value.encode()) == request['text_sha256']]
    if len(anchors) != 1:
        raise RoomError('Native caller correlation is missing or ambiguous')
    anchor = anchors[0]
    if workspace is not None and anchor.get('cwd') != workspace:
        raise RoomError('Native reviewer caller workspace contradicts its retained owner')
    start = timestamp(anchor['timestamp'])
    following = [x for x in rows if timestamp(x['timestamp']) > start and human_text(x) is not None]
    end = timestamp(following[0]['timestamp']) if following else float('inf')
    errors, settled_errors, stops = [], [], {}
    for row in rows:
        if not start <= timestamp(row['timestamp']) < end or row.get('type') != 'assistant':
            continue
        message = row.get('message')
        if not isinstance(message, dict):
            raise RoomError('Malformed native assistant evidence')
        if workspace is not None:
            if row.get('cwd') != workspace:
                raise RoomError('Native reviewer response workspace contradicts its retained owner')
            if (row.get('isApiErrorMessage') is not True and message.get('model') != '<synthetic>'
                    and message.get('model') != request['model']):
                raise RoomError('Native response model contradicts the owned request')
        if row.get('isApiErrorMessage') is True:
            nested = _api_error_nested(row)
            if not isinstance(nested, dict):
                raise RoomError('Malformed native API error')
            status = row.get('apiErrorStatus', nested.get('status'))
            errors.append({'uuid': row['uuid'], 'error': row.get('error'), 'http_status': status})
        elif message.get('model') != '<synthetic>' and message.get('stop_reason'):
            if family is None:
                if message.get('model') != request['model']:
                    raise RoomError('Native response model contradicts the owned request')
            else:
                # Every attributable non-synthetic root stop row is collected before any mismatch is
                # evaluated, so a qualified expectation retains the exact observed identities.
                observed.add(message.get('model'))
                stop_rows.append({'uuid': row['uuid'], 'model': message.get('model'),
                                  'stop_reason': message['stop_reason']})
                if qualified is None and (not model_matches(request, message.get('model')) or len(observed) > 1):
                    raise RoomError('Native response model contradicts the owned family request')
            stops[message.get('id')] = message['stop_reason']
            if message['stop_reason'] == 'end_turn':
                settled_errors.extend(errors)
                errors = []
                stops = {message.get('id'): 'end_turn'}
    result = {'anchor_uuid': anchor['uuid'], 'next_human_uuid': following[0]['uuid'] if following else None,
              'errors': errors, 'settled_errors': settled_errors, 'stop_reasons': list(stops.values())}
    if family is not None:  # Exact requests keep their historical result shape and digests.
        result['observed_models'] = sorted(observed)
    if qualified is not None:
        expected, qualification_sha256 = qualified
        result['expected_model'] = expected
        result['qualification_sha256'] = qualification_sha256
        result['stop_row_ids'] = sorted(row['uuid'] for row in stop_rows)
        result['stop_row_reasons'] = sorted(set(row['stop_reason'] for row in stop_rows))
        if sorted(observed) and sorted(observed) != [expected]:
            # A qualified family must observe exactly its pre-inference expected model. The
            # contradiction retains every attributable actual identity; it never falls back, and a
            # missing observation is handled by the outcome classifier as an unknown hold.
            result['model_contradiction'] = {'expected_model': expected, 'observed_models': sorted(observed),
                                             'stop_row_ids': sorted(row['uuid'] for row in stop_rows),
                                             'qualification_sha256': qualification_sha256}
    return result



def compaction_imports(events, session_id, snapshot):
    """Recognize only exact native compaction records imported by AO as history turns."""
    from ao_project_room import digest
    namespace = snapshot.get('activeBranchId')
    if not isinstance(namespace, str):
        return []
    summaries = {}
    for row in events:
        if (row.get('type') != 'user' or row.get('sessionId') != session_id or row.get('isSidechain')
                or row.get('isCompactSummary') is not True or row.get('isVisibleInTranscriptOnly') is not True):
            continue
        message = row.get('message')
        content = message.get('content') if isinstance(message, dict) else None
        if isinstance(content, list) and content and all(isinstance(x, dict) and x.get('type') == 'text'
                and isinstance(x.get('text'), str) for x in content):
            content = ''.join(x['text'] for x in content)
        identity = row.get('uuid')
        if not isinstance(identity, str) or not identity or not isinstance(content, str) or not content:
            continue
        provider_id = 'acp-history-turn:' + str(len(namespace.encode())) + ':' + namespace + str(len(identity.encode())) + ':' + identity
        proof = (identity, digest(content.encode()))
        if provider_id in summaries and summaries[provider_id] != proof:
            raise RoomError('Conflicting duplicate native compaction identity')
        summaries[provider_id] = proof
    result = []
    for turn in snapshot.get('turns', []):
        match = summaries.get(turn.get('providerTurnId'))
        if turn.get('state') != 'recovered' or not match:
            continue
        messages = [m for m in snapshot.get('messages', []) if m.get('turnId') == turn.get('id')]
        if (len(messages) != 1 or messages[0].get('role') != 'user' or messages[0].get('origin') != 'human'
                or messages[0].get('streaming') or not isinstance(messages[0].get('text'), str)
                or digest(messages[0]['text'].encode()) != match[1]):
            continue
        result.append({'turn_id': turn['id'], 'provider_turn_id': turn['providerTurnId'], 'message_id': messages[0]['id'],
                       'native_uuid': match[0], 'text_sha256': match[1], 'turn_sha256': digest(turn), 'messages_sha256': digest(messages)})
    return sorted(result, key=lambda x: x['turn_id'])


def _failed_task_notification(row, session_id, workspace):
    """Recognize one observed SDK envelope; its text is never task-result authority."""
    if (row.get('type') != 'user' or row.get('sessionId') != session_id or row.get('cwd') != workspace
            or row.get('isSidechain') is not False or row.get('origin') != {'kind': 'task-notification'}
            or row.get('promptSource') != 'sdk' or row.get('queueSkipAttachments') is not True
            or row.get('userType') != 'external' or row.get('entrypoint') != 'sdk-ts'
            or any(key in row for key in ('isCompactSummary', 'isVisibleInTranscriptOnly'))
            or any(row.get(key) is not None for key in ('agentId', 'agent_id'))):
        return None
    message = row.get('message')
    content = message.get('content') if isinstance(message, dict) and message.get('role') == 'user' else None
    if (not isinstance(content, str) or not re.match(r'<task-notification>\r?\n', content)
            or not re.search(r'\r?\n</task-notification>\Z', content) or '<!' in content or '<?' in content):
        return None
    try:
        parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True, insert_pis=True))
        root = ET.fromstring(content, parser=parser)
    except ET.ParseError:
        return None
    fields = ('task-id', 'tool-use-id', 'output-file', 'status', 'summary', 'note')
    if (root.tag != 'task-notification' or root.attrib or root.text != '\n' or root.tail
            or tuple(child.tag for child in root) != fields
            or any(child.attrib or len(child) or not isinstance(child.text, str) or not child.text
                   or child.tail != '\n' for child in root)
            or not re.fullmatch(r'[A-Za-z0-9]+', root[0].text)
            or not re.fullmatch(r'toolu_[A-Za-z0-9]+', root[1].text)
            or root[3].text != 'failed'):
        return None
    return content


def task_notification_imports(events, session_id, snapshot, workspace):
    """Prove recovered notification history, never worker completion or new caller authority."""
    from ao_project_room import digest
    if not isinstance(events, list) or any(not isinstance(row, dict) for row in events):
        raise RoomError('Task-notification native events are malformed')
    namespace = snapshot.get('activeBranchId')
    if snapshot.get('history_truncated') is not False or any(not isinstance(value, str) or not value for value in
           (session_id, workspace, namespace, snapshot.get('sessionId'), snapshot.get('conversationId'))):
        return []
    counts = {}
    for row in events:
        identity = row.get('uuid')
        if row.get('sessionId') == session_id and isinstance(identity, str):
            counts[identity] = counts.get(identity, 0) + 1
    candidates = {}
    for row in events:
        content = _failed_task_notification(row, session_id, workspace)
        identity = row.get('uuid')
        if content is None:
            continue
        try:
            if not isinstance(identity, str) or str(uuid.UUID(identity)) != identity:
                continue
        except ValueError:
            continue
        provider_id = 'acp-history-turn:' + str(len(namespace.encode())) + ':' + namespace + str(len(identity.encode())) + ':' + identity
        candidates[provider_id] = (row, content)
    turns, messages = snapshot.get('turns'), snapshot.get('messages')
    matching = {turn['providerTurnId'] for turn in (turns if isinstance(turns, list) else [])
                if isinstance(turn, dict) and turn.get('state') == 'recovered'
                and isinstance(turn.get('providerTurnId'), str) and turn['providerTurnId'] in candidates}
    if not matching:
        # Unimported notifications do not change historical observation rules.
        # Enforce this proof's extra uniqueness checks only for an import candidate.
        return []
    if any(counts[candidates[provider_id][0]['uuid']] != 1 for provider_id in matching):
        raise RoomError('Duplicate native task-notification identity')
    if (not isinstance(turns, list) or not isinstance(messages, list)
            or any(not isinstance(item, dict) for item in turns + messages)):
        raise RoomError('Task-notification history requires complete turn and message arrays')
    for label, values in (('turn', [turn.get('id') for turn in turns]),
                          ('provider turn', [turn.get('providerTurnId') for turn in turns]),
                          ('message', [message.get('id') for message in messages])):
        if any(not isinstance(value, str) or not value for value in values) or len(set(values)) != len(values):
            raise RoomError('Task-notification history has ambiguous ' + label + ' identities')
    result = []
    for turn in turns:
        candidate = candidates.get(turn.get('providerTurnId'))
        if turn.get('state') != 'recovered' or candidate is None:
            continue
        row, content = candidate
        imported = [message for message in messages if message.get('turnId') == turn['id']]
        if (len(imported) != 1 or imported[0].get('role') != 'user' or imported[0].get('origin') != 'human'
                or imported[0].get('streaming') is not False or imported[0].get('text') != content):
            continue
        result.append({'kind': 'task_notification_history_import', 'version': 1, 'notification_status': 'failed',
                       'session_id': snapshot['sessionId'], 'native_session_id': session_id,
                       'conversation_id': snapshot['conversationId'], 'branch_id': namespace,
                       'turn_id': turn['id'], 'provider_turn_id': turn['providerTurnId'], 'message_id': imported[0]['id'],
                       'native_uuid': row['uuid'], 'native_event_sha256': digest(row), 'text_sha256': digest(content.encode()),
                       'turn_sha256': digest(turn), 'messages_sha256': digest(imported)})
    return sorted(result, key=lambda proof: proof['turn_id'])


def source_keys(role):
    if role == 'engineer':
        return 'native_outcome_source', 'native_outcome_source_evidence'
    if role == 'reviewer':
        return 'native_reviewer_outcome_source', 'native_reviewer_outcome_source_evidence'
    raise RoomError('Native outcome source requires an exact engineer or reviewer role')


def validate_source(state, source, role='engineer'):
    from ao_native_identity import read_owner
    key, _ = source_keys(role)
    expected = {'database', 'transcript', 'session_id', 'native_session_id'}
    if role == 'reviewer':
        expected.add('workspace_path')
    if not isinstance(source, dict) or set(source) != expected:
        raise RoomError('Native outcome source has an unexpected shape')
    binding = state['bindings'].get(role, {})
    if binding.get('harness') != 'claude-code' or source['session_id'] != binding.get('session_id'):
        raise RoomError('Native outcome source is not the bound Claude ' + role)
    try:
        owner = read_owner(source['database'], binding['session_id'])
    except (ValueError, OSError) as exc:
        raise RoomError('Cannot establish the retained native outcome owner') from exc
    if (owner['project_id'] != state['ao_project_id']
            or owner['provider_conversation_id'] != source['native_session_id']
            or owner['ao_conversation_id'] != binding.get('conversation_id')
            or owner['active_branch_id'] != binding.get('branch_id')):
        raise RoomError('Native outcome owner changed')
    if role == 'reviewer':
        if (state.get('workflow') != 'astra_led' or state.get('spec_review_extension')
                or binding.get('reasoning_effort') != 'max'
                or any(other != role and value.get('session_id') == binding['session_id']
                       for other, value in state['bindings'].items())
                or owner['ao_conversation_id'] != binding.get('conversation_id')
                or owner['active_branch_id'] != binding.get('branch_id')
                or owner['workspace_path'] != source['workspace_path']
                or not isinstance(source['workspace_path'], str) or not Path(source['workspace_path']).is_absolute()):
            raise RoomError('Native reviewer owner/workspace contradicts its exact Astra-led binding at MAX')
        previous = state.get(key)
        if previous is not None and (not isinstance(previous, dict) or set(previous) != expected
                or any(previous[k] != source[k] for k in ('session_id', 'native_session_id', 'workspace_path'))):
            raise RoomError('Native reviewer source cannot replace its retained owner or workspace')
    return owner


MAX_TRANSCRIPT_BYTES = 256_000_000          # complete-history physical cap, bytes
# Headroom, estimated not measured: one Claude Code row can carry several base64 images (API images
# run up to 5 MB each; AO browser screenshots read through the Read tool land in tool_result rows),
# so the previous 4 MiB cap risked refusing legitimate histories that the old 64,000,000-byte
# whole-file read accepted. 16 MiB still bounds one record's pending buffer and parse while
# MAX_TRANSCRIPT_BYTES bounds the total.
MAX_TRANSCRIPT_RECORD_BYTES = 16_777_216     # one JSONL record (line) excluding its terminator
MAX_TRANSCRIPT_RECORDS = 2_000_000          # non-blank records
TRANSCRIPT_READ_CHUNK = 1_048_576
TRANSCRIPT_RECORD_BYTES_REFUSAL = 'Native transcript record exceeds the finite record budget'
TRANSCRIPT_RECORD_COUNT_REFUSAL = 'Native transcript exceeds the finite record count budget'
TRANSCRIPT_MALFORMED_REFUSAL = 'Native transcript contains a malformed record'
_read_chunk = os.read  # module attribute, test seam only
_LINE_BREAK = re.compile(br'[\r\n]+')


def iter_transcript_records(chunks, *, max_record_bytes=MAX_TRANSCRIPT_RECORD_BYTES,
                            max_records=MAX_TRANSCRIPT_RECORDS, malformed_message=TRANSCRIPT_MALFORMED_REFUSAL):
    """Stream ``(start, end, row)`` records out of an iterable of byte chunks, bytes.splitlines()-exact.

    ``start``/``end`` are absolute byte offsets of the record text (``end`` excludes the line
    terminator: one of ``\\n``, ``\\r``, ``\\r\\n``, including a terminator split across a chunk
    boundary). Blank/whitespace-only lines are skipped and never counted. One record (including an
    unterminated pending tail) longer than ``max_record_bytes`` refuses immediately -- before EOF and
    before parsing -- so one record's memory stays bounded; the per-file non-blank record count is
    checked, in order, before that record is parsed. ``json.loads`` runs exactly as the legacy
    per-line parse (bytes input; a malformed row or pathological nesting refuses as
    ``malformed_message``); a parsed non-dict row is yielded unchanged so each caller keeps its own
    shape refusal.

    Terminator *runs* (one or more consecutive ``\\r``/``\\n`` bytes, in any mixture) are matched and
    skipped in one step rather than one byte at a time, so a flood of bare blank lines no longer costs
    one Python-level loop iteration per character; every zero-length "line" a run collapses was always
    blank and discarded either way, so this changes no yielded row, offset or refusal. A pending
    (unterminated) record that keeps growing across many small chunks is also not rescanned from its
    own start on every new chunk: a carried-forward scan position only ever advances. A whitespace-only
    but non-empty line (for example a lone space before its terminator) still costs one iteration of
    the outer per-record bookkeeping below, because its content must still be sliced and stripped
    before it can be discarded -- but, matching the legacy per-line parse, it is still never counted
    toward ``max_records``.

    The pending buffer for one in-progress (not yet terminator-closed) record is bounded by
    ``max_record_bytes`` plus the size of one more incoming chunk, not by ``max_record_bytes`` alone:
    the oversized-record refusal only fires once a whole chunk has been appended and rescanned, so
    the buffer can briefly hold up to one chunk's worth more than the nominal budget before that is
    caught. The ``max_record_bytes`` budget is checked against every physical line's raw length
    before ``settle`` ever looks at its content, including a whitespace-only line: such a line longer
    than ``max_record_bytes`` still refuses, even though a shorter whitespace-only line is silently
    skipped and never counted toward ``max_records``. ``max_record_bytes`` and ``max_records`` must
    each be a positive int; a non-positive or non-integer one raises ``ValueError`` (a programming
    error, not a transcript defect) the first time this generator is advanced, before any chunk is
    consumed.
    """
    if not isinstance(max_record_bytes, int) or max_record_bytes <= 0:
        raise ValueError('iter_transcript_records requires a positive integer max_record_bytes')
    if not isinstance(max_records, int) or max_records <= 0:
        raise ValueError('iter_transcript_records requires a positive integer max_records')
    buf = b''
    buf_start = 0
    count = 0
    scanned = 0  # prefix length of buf already confirmed free of a terminator run

    def settle(record_bytes, start_abs, end_abs):
        nonlocal count
        if not record_bytes.strip():
            return None
        count += 1
        if count > max_records:
            raise RoomError(TRANSCRIPT_RECORD_COUNT_REFUSAL)
        try:
            row = json.loads(record_bytes)
        except (ValueError, RecursionError) as exc:
            raise RoomError(malformed_message) from exc
        return start_abs, end_abs, row

    for chunk in chunks:
        if not chunk:
            continue
        buf += chunk
        pos = 0
        while True:
            match = _LINE_BREAK.search(buf, scanned if scanned > pos else pos)
            if match is None:
                scanned = len(buf)
                break
            idx = match.start()
            if idx - pos > max_record_bytes:
                raise RoomError(TRANSCRIPT_RECORD_BYTES_REFUSAL)
            record = settle(buf[pos:idx], buf_start + pos, buf_start + idx)
            pos = match.end()
            scanned = pos
            if record is not None:
                yield record
        if len(buf) - pos > max_record_bytes:
            raise RoomError(TRANSCRIPT_RECORD_BYTES_REFUSAL)
        if pos:
            buf = buf[pos:]
            buf_start += pos
            scanned -= pos
    if buf:
        if len(buf) > max_record_bytes:
            raise RoomError(TRANSCRIPT_RECORD_BYTES_REFUSAL)
        record = settle(buf, buf_start, buf_start + len(buf))
        if record is not None:
            yield record


def _transcript_identity_recheck(descriptor, path, before, total=None):
    """True while the open ``descriptor`` and the named ``path`` both still match the ``before`` fstat.

    ``descriptor``'s own fstat must still show the same inode/size/mtime/ctime/uid/mode, and, only
    when ``total`` is given (the read ran to completion), ``total`` must equal ``before.st_size`` as
    well -- a mid-read exception recheck passes ``total=None`` because a short consumption so far is
    not itself a sign of change. Comparing ``st_ctime_ns`` catches a same-size rewrite whose writer
    restores ``st_mtime_ns`` with ``os.utime``; comparing ``st_uid``/``st_mode`` catches a
    ``chown``/``chmod`` racing the read (each of which also bumps ``ctime`` on its own, but is named
    explicitly so the refusal is traceable to the actual change). The named ``path`` is then
    reverified by a separate ``lstat`` (still a regular file, same dev/ino/size/mtime/ctime/uid/mode);
    an ``OSError`` there (removed, unreadable parent, ...) counts as changed, same as a mismatch.
    """
    after = os.fstat(descriptor)
    if ((total is not None and total != before.st_size)
            or (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns, before.st_uid, before.st_mode)
            != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_uid, after.st_mode)):
        return False
    try:
        named = Path(path).lstat()
    except OSError:
        return False
    return (stat.S_ISREG(named.st_mode)
            and (named.st_dev, named.st_ino, named.st_size, named.st_mtime_ns, named.st_ctime_ns, named.st_uid,
                 named.st_mode)
            == (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns, before.st_uid,
                before.st_mode))


def read_complete_transcript(path, *, unsafe_message, changed_message, malformed_message=None,
                             unavailable_message=None, prefix_bytes=None, max_bytes=None,
                             max_record_bytes=None, max_records=None, chunk_bytes=None):
    """Read one complete owned native transcript, streamed and finitely bounded, never ``os.fdopen``.

    The single reader behind every production lane that needs a complete native history: an
    ownership/identity-checked descriptor is opened once, read in ``chunk_bytes`` pieces into
    ``iter_transcript_records`` while an incremental SHA-256 (and, when ``prefix_bytes`` is given, a
    snapshot digest of exactly its first ``prefix_bytes`` bytes) accumulates, then descriptor identity
    is reverified before and after the read and the named path is reverified once more. An identity or
    mutation refusal always uses the caller's own ``unsafe_message``/``changed_message``; a malformed
    row uses ``malformed_message`` (the caller's own when given, else the shared
    ``TRANSCRIPT_MALFORMED_REFUSAL`` default); a record over ``max_record_bytes`` or a file over
    ``max_records`` always raises one of the two shared module refusal strings
    (``TRANSCRIPT_RECORD_BYTES_REFUSAL``/``TRANSCRIPT_RECORD_COUNT_REFUSAL``), never a caller-supplied
    one. Any of ``malformed_message``/``max_bytes``/``max_record_bytes``/``max_records``/
    ``chunk_bytes`` left at its ``None`` default resolves the current module constant at call time
    (not at import time), so patching e.g. ``MAX_TRANSCRIPT_BYTES`` takes effect on the next call.
    A parse refusal (malformed row, record-bytes or record-count budget) from ``iter_transcript_records``
    is itself rechecked against the same identity before it is let through: a concurrent same-size
    rewrite or a writer completing a torn final row after this read's own EOF can turn a row that
    ``before`` never actually contained into one that fails to parse, and that must still be reported
    as ``changed_message``, not misread as a malformed/oversized record of a file that never changed.
    Returns ``{'sha256', 'size', 'records', 'events', 'offsets', 'ends', 'prefix_sha256'}``;
    ``offsets``/``ends`` are ``array.array('q')`` parallel to ``events``, one absolute byte-offset
    pair per yielded record.

    When ``prefix_bytes`` is given but exceeds the file's own size, ``prefix_sha256`` stays
    ``None`` -- there is no complete prefix of that length to digest; the format-recovery lane's own
    commit already refuses on a retained-size mismatch before it would ever rely on that digest, so
    this ``None`` is never mistaken for a valid proof of a short prefix. ``max_bytes``,
    ``max_record_bytes``, ``max_records`` and ``chunk_bytes`` must each resolve to a positive int;
    one that does not raises ``ValueError`` immediately, before ``path`` is even opened, because a
    non-positive or non-integer budget is a programming error (for example ``chunk_bytes=0`` would
    otherwise read zero bytes forever and misreport a healthy file as changed), never a transcript
    defect to report through ``unsafe_message``/``changed_message``.
    """
    if malformed_message is None:
        malformed_message = TRANSCRIPT_MALFORMED_REFUSAL
    if max_bytes is None:
        max_bytes = MAX_TRANSCRIPT_BYTES
    if max_record_bytes is None:
        max_record_bytes = MAX_TRANSCRIPT_RECORD_BYTES
    if max_records is None:
        max_records = MAX_TRANSCRIPT_RECORDS
    if chunk_bytes is None:
        chunk_bytes = TRANSCRIPT_READ_CHUNK
    for _limit_name, _limit_value in (('max_bytes', max_bytes), ('max_record_bytes', max_record_bytes),
                                       ('max_records', max_records), ('chunk_bytes', chunk_bytes)):
        if not isinstance(_limit_value, int) or _limit_value <= 0:
            raise ValueError('read_complete_transcript requires a positive integer ' + _limit_name)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError as exc:
        if unavailable_message is None:
            raise
        raise RoomError(unavailable_message) from exc
    try:
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_mode & 0o022
                or before.st_size > max_bytes):
            raise RoomError(unsafe_message)
        hasher = hashlib.sha256()
        total = 0
        prefix_sha256 = None

        def chunks():
            nonlocal total, prefix_sha256
            while True:
                chunk = _read_chunk(descriptor, chunk_bytes)
                if not chunk:
                    # A short read (fewer bytes than ``before.st_size`` before EOF) is a shrink
                    # mid-observation. Catch it here, before any leftover unterminated buffer is
                    # handed to iter_transcript_records as a final record: a truncation can cut a
                    # record in half, and that half-record must surface as ``changed_message``, never
                    # as a malformed-record refusal.
                    if total != before.st_size:
                        raise RoomError(changed_message)
                    return
                new_total = total + len(chunk)
                if new_total > before.st_size:
                    raise RoomError(changed_message)
                if prefix_bytes is not None and prefix_sha256 is None and total + len(chunk) >= prefix_bytes:
                    split = prefix_bytes - total
                    hasher.update(chunk[:split])
                    prefix_sha256 = hasher.copy().hexdigest()
                    hasher.update(chunk[split:])
                else:
                    hasher.update(chunk)
                total = new_total
                yield chunk

        events, offsets, ends = [], array.array('q'), array.array('q')
        try:
            for start, end, row in iter_transcript_records(chunks(), max_record_bytes=max_record_bytes,
                                                            max_records=max_records, malformed_message=malformed_message):
                events.append(row)
                offsets.append(start)
                ends.append(end)
        except RoomError as exc:
            # The parse refusal itself (malformed row, record-bytes or record-count budget) is only
            # trustworthy against the file ``before`` described; a concurrent change that races the
            # read can manufacture exactly this shape of failure out of bytes that never all
            # coexisted in one consistent file. Re-run the identity check now, descriptor still open,
            # and report the mutation instead of the parse symptom when identity moved.
            if not _transcript_identity_recheck(descriptor, path, before):
                raise RoomError(changed_message) from exc
            raise
        if prefix_bytes is not None and prefix_sha256 is None and prefix_bytes <= total:
            prefix_sha256 = hasher.copy().hexdigest()
        if not _transcript_identity_recheck(descriptor, path, before, total):
            raise RoomError(changed_message)
        return {'sha256': hasher.hexdigest(), 'size': total, 'records': len(events), 'events': events,
                'offsets': offsets, 'ends': ends, 'prefix_sha256': prefix_sha256}
    finally:
        os.close(descriptor)


def preflight_source(directory, state, database, transcript, role='engineer', workspace=None):
    """Pre-dispatch ownership preflight and source registration; no response or native event is required.

    Validates the bound session's exact retained owner through the existing read-only identity API
    and the prospective transcript path this session will write, before any inference exists. The
    ordinary source schema is preserved and a conflicting registered source is never replaced.
    Returns bounded audit evidence; the caller saves the state that now registers the binding.
    """
    from ao_native_identity import read_owner
    if role not in ('engineer', 'reviewer'):
        raise RoomError('Native outcome source requires an exact engineer or reviewer role')
    if not isinstance(database, str) or not database:
        raise RoomError('Pre-dispatch ownership requires the explicit verified AO owner database path')
    if not isinstance(transcript, str) or not transcript:
        raise RoomError('Pre-dispatch ownership requires the prospective native transcript path')
    key, _ = source_keys(role)
    binding = (state.get('bindings') or {}).get(role) or {}
    if not binding or binding.get('harness') != 'claude-code':
        raise RoomError('Pre-dispatch ownership requires the bound Claude ' + role)
    if not isinstance(binding.get('session_id'), str) or not binding['session_id'].strip():
        raise RoomError('Pre-dispatch ownership requires the bound AO session identity')
    if any(not isinstance(binding.get(field), str) or not binding[field].strip()
           for field in ('conversation_id', 'branch_id')):
        raise RoomError('Pre-dispatch ownership requires the bound AO conversation and branch')
    try:
        owner = read_owner(database, binding['session_id'])
    except (ValueError, OSError) as exc:
        raise RoomError('Cannot establish the prospective native outcome owner') from exc
    if owner['project_id'] != state.get('ao_project_id'):
        raise RoomError('Prospective native owner belongs to another AO project')
    if owner['ao_conversation_id'] != binding['conversation_id'] or owner['active_branch_id'] != binding['branch_id']:
        raise RoomError('Prospective native owner contradicts the bound conversation or branch')
    path = Path(transcript)
    if not path.is_absolute():
        raise RoomError('Use the exact absolute prospective native transcript path')
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise RoomError('Use the exact owned native transcript path without symlinks')
    if path.name != owner['provider_conversation_id'] + '.jsonl':
        raise RoomError('Prospective transcript name must be the retained provider conversation identity')
    try:
        parent = path.parent.lstat()
    except OSError as exc:
        raise RoomError('Prospective native transcript directory is unavailable') from exc
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid():
        raise RoomError('Prospective native transcript directory is unsafe')
    if path.exists():
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022
                or info.st_size > MAX_TRANSCRIPT_BYTES):
            raise RoomError('Prospective native transcript file is unsafe')
    if role == 'engineer' and workspace is None:
        from ao_delegates import validate_preparation
        workspace = validate_preparation(directory, state, binding['session_id'], check_routing=False)['worktree']
    if not isinstance(workspace, str) or not Path(workspace).is_absolute():
        raise RoomError('Pre-dispatch ownership requires the exact bound workspace')
    if owner['workspace_path'] != workspace:
        raise RoomError('Prospective native owner workspace contradicts its exact bound preparation')
    source = {'database': database, 'transcript': transcript, 'session_id': binding['session_id'],
              'native_session_id': owner['provider_conversation_id']}
    if role == 'reviewer':
        source['workspace_path'] = workspace
    previous, registered = state.get(key), 'identical'
    if previous is None:
        registered = 'created'
    elif previous != source:
        raise RoomError('A conflicting native outcome source is already registered; it is never replaced')
    try:
        validate_source(state, source, role)
    except RoomError as exc:
        raise RoomError('Prospective native outcome source cannot be validated: ' + str(exc)) from exc
    if previous is None:
        state[key] = source
    return {'version': 1, 'role': role, 'source': source, 'native_owner': owner,
            'transcript_present': path.exists(), 'registered': registered,
            'basis': 'Read-only ownership preflight before dispatch; the native process environment, the actual '
                     'provider and effective effort remain unobserved'}


_UNSET_TRANSCRIPT_SIZE_LIMIT = object()  # module-private sentinel: resolve MAX_TRANSCRIPT_BYTES at call
# time, not at def-time, so a later `mock.patch.object(ao_native_outcome, 'MAX_TRANSCRIPT_BYTES', ...)`
# takes effect on the next call instead of being baked into this function's own defaults tuple.


def validate_registered_source(directory, state, role='engineer',
                               transcript_size_limit=_UNSET_TRANSCRIPT_SIZE_LIMIT):
    """Full read-only verification of one already-registered prospective source before dispatch.

    Uses the existing owner database reader, the bound session and the prepared workspace the turn
    is about to run in. The prospective transcript may not exist yet; when it does, it must be the
    exact owned non-symlink file for the retained provider conversation. This never calls a model
    and never requires a completed response; the first sync still requires the actual complete
    identity proof. ``transcript_size_limit`` bounds the accepted file size; ``None`` lifts it for
    readers that only ever take a bounded tail window; leaving it unset resolves the current
    ``MAX_TRANSCRIPT_BYTES`` at call time, not at this function's definition time.
    """
    if transcript_size_limit is _UNSET_TRANSCRIPT_SIZE_LIMIT:
        transcript_size_limit = MAX_TRANSCRIPT_BYTES
    from ao_native_identity import read_owner
    if role not in ('engineer', 'reviewer'):
        raise RoomError('Native outcome source requires an exact engineer or reviewer role')
    key, _ = source_keys(role)
    expected = {'database', 'transcript', 'session_id', 'native_session_id'}
    if role == 'reviewer':
        expected.add('workspace_path')
    source = state.get(key)
    binding = (state.get('bindings') or {}).get(role) or {}
    if (not isinstance(source, dict) or set(source) != expected or binding.get('harness') != 'claude-code'
            or source.get('session_id') != binding.get('session_id')):
        raise RoomError('Native outcome source is not the bound Claude ' + role)
    try:
        owner = read_owner(source['database'], binding['session_id'])
    except (ValueError, OSError) as exc:
        raise RoomError('Cannot establish the retained native outcome owner') from exc
    if owner['project_id'] != state.get('ao_project_id'):
        raise RoomError('Prospective native owner belongs to another AO project')
    if (owner['ao_conversation_id'] != binding.get('conversation_id')
            or owner['active_branch_id'] != binding.get('branch_id')
            or owner['provider_conversation_id'] != source['native_session_id']):
        raise RoomError('Prospective native owner contradicts the bound conversation, branch or provider identity')
    path = Path(source['transcript'])
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise RoomError('Use the exact owned native transcript path without symlinks')
    if path.name != owner['provider_conversation_id'] + '.jsonl':
        raise RoomError('Prospective transcript name must be the retained provider conversation identity')
    try:
        parent = path.parent.lstat()
    except OSError as exc:
        raise RoomError('Prospective native transcript directory is unavailable') from exc
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid():
        raise RoomError('Prospective native transcript directory is unsafe')
    if path.exists():
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022
                or (transcript_size_limit is not None and info.st_size > transcript_size_limit)):
            raise RoomError('Prospective native transcript file is unsafe')
    if role == 'engineer':
        from ao_delegates import validate_preparation
        workspace = validate_preparation(directory, state, binding['session_id'], check_routing=False)['worktree']
    else:
        workspace = source.get('workspace_path')
        if not isinstance(workspace, str) or not Path(workspace).is_absolute():
            raise RoomError('Native reviewer source requires its exact owner workspace')
    if owner['workspace_path'] != workspace:
        raise RoomError('Prospective native owner workspace contradicts its exact bound preparation')
    return {'version': 1, 'role': role, 'source': dict(source), 'native_owner': owner,
            'transcript_present': path.exists(),
            'basis': 'Read-only ownership verification before dispatch; the native process environment, the actual '
                     'provider and effective effort remain unobserved'}


def inspect(directory, state, request, source, snapshot):
    role = request.get('role', 'engineer')
    owner = validate_source(state, source) if role == 'engineer' else validate_source(state, source, role)
    if role == 'reviewer':
        binding = state['bindings']['reviewer']
        if (any(request.get(k) != binding.get(k) for k in
                ('session_id', 'harness', 'model', 'reasoning_effort', 'conversation_id', 'branch_id'))
                or request.get('model_reroute') or source['session_id'] != request['session_id']
                or snapshot.get('sessionId') != request['session_id']
                or (snapshot.get('settings') or {}).get('model') != request['model']
                or (snapshot.get('settings') or {}).get('reasoningEffort') != request['reasoning_effort']
                or snapshot.get('history_truncated')):
            raise RoomError('Native reviewer request/model/history contradicts its exact binding')
        workspace = source['workspace_path']
    else:
        # A family request's frozen expectation is trusted only as its recorded engineering epoch requires.
        # Exact requests keep their unchanged exact-equality inspection.
        import ao_engineering_model
        if ao_engineering_model._family_of(request) is not None:
            ao_engineering_model.check_frozen(directory, state, request)
        # Engineer workspace retains its original immutable preparation proof.
        from ao_delegates import validate_preparation
        prepared = validate_preparation(directory, state, request['session_id'], check_routing=False)
        workspace = prepared['worktree']
    if (owner['workspace_path'] != workspace or owner['ao_conversation_id'] != snapshot.get('conversationId')
            or owner['active_branch_id'] != snapshot.get('activeBranchId')):
        raise RoomError('Native outcome workspace/conversation identity changed')
    path = Path(source['transcript'])
    if (not path.is_absolute() or path.name != source['native_session_id'] + '.jsonl'
            or any(p.is_symlink() for p in (path, *path.parents))):
        raise RoomError('Use the exact owned native transcript without symlinks')
    reading = read_complete_transcript(path, unsafe_message='Native transcript ownership or size is unsafe',
                                       changed_message='Native transcript changed while being observed')
    events = reading['events']
    result = events_outcome(events, request, source['native_session_id'],
                            workspace=workspace if role == 'reviewer' else None)
    if role == 'engineer':
        proof = compaction_failure(events, request, source['native_session_id'], workspace, result)
        if proof is not None:
            result = {**result, 'compaction_failure': proof}
    result = {**result, 'source': source, 'source_sha256': reading['sha256'],
              'compaction_imports': compaction_imports(events, source['native_session_id'], snapshot)}
    if result.get('model_contradiction') is not None:
        # The observed identities, the expected qualification digest and the exact source digest are
        # always retained. With otherwise complete evidence — no unsettled errors, the owned turn
        # ended at end_turn, every attributable stop row is tool_use or end_turn (a hidden earlier
        # refusal or any other stop settles nothing), and every observed identity is an exact
        # identifier of the expected model's family — the contradiction is a settled diagnosed
        # model_mismatch rather than unknown. Anything else keeps the existing unknown/hold path.
        contradiction = {**result['model_contradiction'], 'source_sha256': result['source_sha256']}
        expected = contradiction['expected_model']
        match = re.fullmatch(r'claude-([a-z]+)(?:-[0-9]+)+', expected) if isinstance(expected, str) else None
        observed = contradiction['observed_models']
        import ao_engineering_model
        complete = (match is not None and result['errors'] == []
                    and result['stop_reasons'] == ['end_turn']
                    and all(reason in ('tool_use', 'end_turn') for reason in result['stop_row_reasons'])
                    and bool(observed)
                    and all(ao_engineering_model.family_member(match.group(1), model)
                            for model in observed))
        if complete:
            result = {**result, 'model_contradiction': contradiction,
                      'model_mismatch': dict(contradiction)}
        else:
            result = {**result, 'model_contradiction': contradiction,
                      'unknown': 'Native response model contradicts the qualified expected model for this '
                                            'family; the observed identities and their source evidence are retained'}
    if (role == 'engineer' and state.get('workflow') == 'fable_engineering'
            and snapshot.get('sessionId') == source['session_id'] == request['session_id']):
        notifications = task_notification_imports(events, source['native_session_id'], snapshot, workspace)
        if notifications:  # Do not change unrelated historical observation/digest shapes.
            result['task_notification_imports'] = notifications
    return result
