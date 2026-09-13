"""Bounded local Claude error evidence. Reads no account settings or credentials."""
from datetime import datetime
import json
import os
from pathlib import Path
import stat

from room import RoomError


def timestamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


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


def events_outcome(events, request, session_id):
    from ao_project_room import digest
    if (not isinstance(events, list) or any(not isinstance(x, dict) for x in events)
            or digest(request['text'].encode()) != request['text_sha256']):
        raise RoomError('Malformed native evidence or changed caller bytes')
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
        if row.get('isApiErrorMessage') is True:
            nested = row.get('apiError') or {}
            if not isinstance(nested, dict):
                raise RoomError('Malformed native API error')
            status = row.get('apiErrorStatus', nested.get('status'))
            errors.append({'uuid': row['uuid'], 'error': row.get('error'), 'http_status': status})
        elif message.get('model') != '<synthetic>' and message.get('stop_reason'):
            if message.get('model') != request['model']:
                raise RoomError('Native response model contradicts the owned request')
            stops[message.get('id')] = message['stop_reason']
            if message['stop_reason'] == 'end_turn':
                settled_errors.extend(errors)
                errors = []
                stops = {message.get('id'): 'end_turn'}
    return {'anchor_uuid': anchor['uuid'], 'next_human_uuid': following[0]['uuid'] if following else None,
            'errors': errors, 'settled_errors': settled_errors, 'stop_reasons': list(stops.values())}



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


def validate_source(state, source):
    from ao_native_identity import read_owner
    if not isinstance(source, dict) or set(source) != {'database', 'transcript', 'session_id', 'native_session_id'}:
        raise RoomError('Native outcome source has an unexpected shape')
    binding = state['bindings'].get('engineer', {})
    if binding.get('harness') != 'claude-code' or source['session_id'] != binding.get('session_id'):
        raise RoomError('Native outcome source is not the bound Claude engineer')
    try:
        owner = read_owner(source['database'], binding['session_id'])
    except (ValueError, OSError) as exc:
        raise RoomError('Cannot establish the retained native outcome owner') from exc
    if (owner['project_id'] != state['ao_project_id']
            or owner['provider_conversation_id'] != source['native_session_id']):
        raise RoomError('Native outcome owner changed')
    return owner


def inspect(directory, state, request, source, snapshot):
    from ao_project_room import digest
    owner = validate_source(state, source)
    # Workspace comes from the immutable preparation, not an inferred project directory.
    from ao_delegates import validate_preparation
    prepared = validate_preparation(directory, state, request['session_id'], check_routing=False)
    if (owner['workspace_path'] != prepared['worktree'] or owner['ao_conversation_id'] != snapshot.get('conversationId')
            or owner['active_branch_id'] != snapshot.get('activeBranchId')):
        raise RoomError('Native outcome workspace/conversation identity changed')
    path = Path(source['transcript'])
    if (not path.is_absolute() or path.name != source['native_session_id'] + '.jsonl'
            or any(p.is_symlink() for p in (path, *path.parents))):
        raise RoomError('Use the exact owned native transcript without symlinks')
    with path.open('rb') as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_mode & 0o022
                or before.st_size > 64_000_000):
            raise RoomError('Native transcript ownership or size is unsafe')
        raw = stream.read(64_000_001)
        after = os.fstat(stream.fileno())
    if (len(raw) != before.st_size or (before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_ino, after.st_size, after.st_mtime_ns)):
        raise RoomError('Native transcript changed while being observed')
    events = [json.loads(line) for line in raw.splitlines() if line.strip()]
    result = events_outcome(events, request, source['native_session_id'])
    return {**result, 'source': source, 'source_sha256': digest(raw),
            'compaction_imports': compaction_imports(events, source['native_session_id'], snapshot)}
