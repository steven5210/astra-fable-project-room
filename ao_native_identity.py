"""Optional read-only operator evidence for AO 0.12.12's retained session storage.

Public AO observations remain required separately. This module does not call AO,
operate native lifecycles, select a database implicitly, or read transcripts.
"""
from pathlib import Path
import sqlite3
import stat
import os

QUERY = '''SELECT s.id, s.project_id, s.harness, s.session_mode,
 s.is_terminated, s.activity_state, s.workspace_path,
 s.provider_conversation_id, s.controller_generation,
 c.id AS ao_conversation_id, c.active_branch_id,
 b.provider_conversation_id AS branch_provider_conversation_id,
 b.session_id AS branch_session_id, b.strategy, b.replay_truncated
 FROM sessions AS s
 JOIN conversations AS c ON c.current_session_id=s.id
 JOIN conversation_branches AS b ON b.id=c.active_branch_id AND b.conversation_id=c.id
 WHERE s.id=?'''


def read_owner(database, session_id):
    path = Path(database)
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Use the verified absolute AO database path without symlinks')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError('AO database ownership is unsafe')
    try:
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute('BEGIN')
            rows = connection.execute(QUERY, (session_id,)).fetchall()
    except sqlite3.Error as exc:
        raise ValueError('AO owner storage is unavailable or its schema changed') from exc
    if len(rows) != 1:
        raise ValueError('AO native owner evidence is missing or ambiguous')
    return _validated(dict(rows[0]), session_id)


def _validated(owner, session_id):
    if not isinstance(owner, dict) or set(owner) != set(QUERY_COLUMNS):
        raise ValueError('AO owner evidence has an unexpected shape')
    if (owner['id'] != session_id or owner['branch_session_id'] != session_id
            or type(owner['is_terminated']) is not int or owner['is_terminated'] != 0 or owner['harness'] != 'claude-code'
            # AO normalizes only the legacy empty strategy to native. Keep the
            # raw value in the evidence; never normalize an unknown nonempty one.
            or owner['session_mode'] != 'chat' or owner['strategy'] not in ('', 'native')
            or type(owner['replay_truncated']) is not int or owner['replay_truncated'] != 0
            or not isinstance(owner['controller_generation'], str) or not owner['controller_generation'].strip()
            or not isinstance(owner['provider_conversation_id'], str) or not owner['provider_conversation_id']
            or owner['branch_provider_conversation_id'] != owner['provider_conversation_id']
            or any(not isinstance(owner[k], str) or not owner[k] for k in ('project_id', 'workspace_path', 'ao_conversation_id', 'active_branch_id'))):
        raise ValueError('AO storage does not prove a retained native Claude owner')
    return owner


def verify_resume(before, after):
    _validated(before, before.get('id') if isinstance(before, dict) else None)
    _validated(after, after.get('id') if isinstance(after, dict) else None)
    stable = set(before) - {'activity_state', 'controller_generation', 'strategy'}
    if (set(before) != set(after) or stable != set(QUERY_COLUMNS) - {'activity_state', 'controller_generation', 'strategy'}
            or any(before[k] != after[k] for k in stable)
            or after['controller_generation'] == before['controller_generation']):
        raise ValueError('AO did not preserve the exact native owner with a new controller generation')
    return {'retained_native_identity': True, 'before_generation': before['controller_generation'],
            'after_generation': after['controller_generation'],
            'meaning': 'Read-only local storage comparison; corroborate current public AO controller/history and MCP connection separately.'}


QUERY_COLUMNS = ('id', 'project_id', 'harness', 'session_mode', 'is_terminated', 'activity_state', 'workspace_path',
                 'provider_conversation_id', 'controller_generation', 'ao_conversation_id', 'active_branch_id',
                 'branch_provider_conversation_id', 'branch_session_id', 'strategy', 'replay_truncated')
