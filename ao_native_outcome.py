"""Bounded local Claude error evidence. Reads no account settings or credentials."""
from datetime import datetime
import json
import os
from pathlib import Path
import re
import stat
import uuid
import xml.etree.ElementTree as ET

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


def events_outcome(events, request, session_id, workspace=None):
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
            or owner['provider_conversation_id'] != source['native_session_id']):
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


def inspect(directory, state, request, source, snapshot):
    from ao_project_room import digest
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
    result = events_outcome(events, request, source['native_session_id'],
                            workspace=workspace if role == 'reviewer' else None)
    result = {**result, 'source': source, 'source_sha256': digest(raw),
              'compaction_imports': compaction_imports(events, source['native_session_id'], snapshot)}
    if (role == 'engineer' and state.get('workflow') == 'fable_engineering'
            and snapshot.get('sessionId') == source['session_id'] == request['session_id']):
        notifications = task_notification_imports(events, source['native_session_id'], snapshot, workspace)
        if notifications:  # Do not change unrelated historical observation/digest shapes.
            result['task_notification_imports'] = notifications
    return result
