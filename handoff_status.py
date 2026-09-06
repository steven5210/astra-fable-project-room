"""Allowlisted current saved handoff projection; no execution or reconciliation."""
import datetime as dt
import re
import recovery
import room

PHASES = frozenset(('prepared', 'preparing', 'running_model', 'running_gates', 'blocked', 'awaiting_astra_review',
                    'accepted', 'changes_required', 'correction_pending', 'scope_change', 'recovery_prepared'))
MAX_GATES = 64
MAX_LINEAGE = 16


def token(value, lengths=(64,)):
    return value if isinstance(value, str) and len(value) in lengths and re.fullmatch('[0-9a-f]+', value) else None


def integer(value, minimum=0):
    return value if type(value) is int and value >= minimum else None


def timestamp(value):
    try:
        return room.parse_timestamp(value).astimezone(dt.timezone.utc).replace(microsecond=0).strftime('%Y-%m-%dT%H:%M:%SZ')
    except (room.RoomError, TypeError, ValueError, AttributeError):
        return None


def choice(value, options):
    return value if isinstance(value, str) and value in options else None


def candidate(value):
    if not isinstance(value, dict):
        return None
    return {'head': token(value.get('head'), (40, 64)), 'sha256': token(value.get('sha256')),
            'path_count': len(value['entries']) if isinstance(value.get('entries'), list) else None}


def project(room_id, manifest, state):
    """Construct new objects at every boundary; never forward arbitrary saved dictionaries."""
    review = state.get('astra_review') if isinstance(state.get('astra_review'), dict) else None
    acceptance = None if review is None else {
        'reviewer': choice(review.get('reviewer'), ('astra',)),
        'accepted': review.get('accepted') if type(review.get('accepted')) is bool else None,
        'recorded_at': timestamp(review.get('recorded_at')), 'spec_revision': integer(review.get('spec_revision'), 1),
        'spec_sha256': token(review.get('spec_sha256')), 'candidate_sha256': token(review.get('candidate_sha256'))}
    gates = state.get('gate_results') if isinstance(state.get('gate_results'), list) else []
    history = state.get('recovery_history') if isinstance(state.get('recovery_history'), list) else []
    active = state.get('recovery') if isinstance(state.get('recovery'), dict) else None
    def edge(value):
        value = value if isinstance(value, dict) else {}
        return {'recovery_id': token(value.get('recovery_id'), (32,)),
                'predecessor_job_id': token(value.get('predecessor_job_id'), (32,)),
                'successor_job_id': token(value.get('successor_job_id'), (32,)),
                'predecessor_attempt': integer(value.get('predecessor_attempt'), 1),
                'kind': choice(value.get('kind'), ('model_timeout', 'session_usage_limit')),
                'launch_state': choice(value.get('launch_state'), ('not_started', 'launched', 'unknown')),
                'status': choice(value.get('status'), ('prepared', 'dispatched', 'consumed', 'launched', 'invalidated')),
                'at': timestamp(value.get('at') or value.get('prepared_at')),
                'launched_at': timestamp(value.get('launched_at'))}
    return {'schema_version': 1, 'meaning': 'current_saved_handoff_record', 'room_id': room_id,
            'handoff_id': token(manifest.get('handoff_id')), 'spec_revision': integer(manifest.get('revision'), 1),
            'spec_sha256': token(manifest.get('spec_sha256')), 'baseline_commit': token(manifest.get('baseline_commit'), (40, 64)),
            'phase': choice(state.get('phase'), PHASES) or 'unknown', 'attempt': integer(state.get('attempt_count'), 1),
            'astra_accepted': state.get('astra_accepted') if type(state.get('astra_accepted')) is bool else None,
            'acceptance': acceptance, 'gates_passed': state.get('gates_passed') if type(state.get('gates_passed')) is bool else None,
            'candidate': candidate(state.get('candidate')), 'gates': [
                {'index': index + 1, 'return_code': gate.get('return_code') if type(gate.get('return_code')) is int and -255 <= gate['return_code'] <= 255 else None,
                 'started_at': timestamp(gate.get('started_at')), 'finished_at': timestamp(gate.get('finished_at')),
                 'stdout_sha256': token(gate.get('stdout_sha256')), 'stderr_sha256': token(gate.get('stderr_sha256'))}
                for index, gate in enumerate(gates[:MAX_GATES]) if isinstance(gate, dict)],
            'gates_truncated': len(gates) > MAX_GATES,
            'lineage': {'active_recovery': edge(active) if active else None,
                        'history': [edge(item) for item in history[-MAX_LINEAGE:]], 'truncated': len(history) > MAX_LINEAGE}}


def load(room_id, handoff_id, path):
    import implementation
    try:
        root, _, manifest, state, _ = recovery.bind_handoff(path)
        with root:
            if manifest.get('handoff_id') != handoff_id:
                raise ValueError('binding')
            return project(room_id, manifest, state)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError,
            recovery.ObservationError, implementation.ImplementationError):
        raise room.RoomError('Saved handoff integrity check failed') from None
