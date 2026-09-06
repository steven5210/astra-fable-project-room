"""Small advisory worker liveness records. Never owns delivery, deadlines or leases."""
import datetime as dt
import json
import os
from pathlib import Path
import re
import time

import recovery
import room

CADENCE_SECONDS = 5.0
MAX_BYTES = 4096
MEANING = 'worker_liveness_only'


def _identifier(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{32}', value) is not None


def _time(value):
    try:
        return room.parse_timestamp(value).astimezone(dt.timezone.utc)
    except (room.RoomError, TypeError, ValueError, AttributeError):
        return None


def unavailable(reason):
    return {'available': False, 'reported_at': None, 'attempt': None, 'meaning': MEANING, 'unavailable_reason': reason}


def observe(jobs_root, job, execution, now, attempt=None):
    if job.get('status') != 'running':
        return unavailable('queued' if job.get('status') == 'queued' else 'terminal')
    if not isinstance(execution, dict) or not _identifier(execution.get('execution_id')):
        return unavailable('legacy_worker')
    if not _identifier(job.get('id')):
        return unavailable('ownership_mismatch')
    try:
        with recovery.OwnedRoot(Path(jobs_root), kind='heartbeat') as root:
            raw = recovery.read_owned(root.path / job['id'] / 'heartbeat.json', MAX_BYTES, 'heartbeat', root)
        value = json.loads(raw)
    except recovery.ObservationError as exc:
        return unavailable('missing' if exc.reason == 'heartbeat_missing' else 'oversized' if exc.reason == 'heartbeat_oversized' else 'unsafe_or_unreadable')
    except (OSError, ValueError, UnicodeError, RecursionError):
        return unavailable('malformed')
    if not isinstance(value, dict) or type(value.get('schema_version')) is not int or value['schema_version'] != 1:
        return unavailable('malformed')
    started = _time(job.get('started_at'))
    if (value.get('job_id') != job['id'] or value.get('execution_id') != execution['execution_id']
            or started is None or _time(value.get('job_started_at')) != started
            or _time(execution.get('started_at')) != started):
        return unavailable('ownership_mismatch')
    recorded = _time(value.get('reported_at'))
    if recorded is None or recorded < started:
        return unavailable('malformed')
    if recorded > now:
        return unavailable('future_timestamp')
    attributed = value.get('attempt')
    if attributed is not None and (type(attributed) is not int or attributed < 1 or attributed != attempt):
        return unavailable('attempt_mismatch')
    return {'available': True, 'reported_at': recorded.replace(microsecond=0).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'attempt': attributed, 'meaning': MEANING, 'unavailable_reason': None}


class Emitter:
    """Called from the existing worker supervision loop, with a monotonic cadence and no extra thread."""
    def __init__(self, jobs_root, job, execution_id, attempt_reader=lambda: None, monotonic=time.monotonic, clock=room.now):
        self.jobs_root = Path(jobs_root)
        self.job = job
        self.execution_id = execution_id
        self.attempt_reader = attempt_reader
        self.monotonic, self.clock = monotonic, clock
        self.last = None
        self.closed = False
        self.identity = None

    def pulse(self, final=False):
        if self.closed:
            return False
        tick = self.monotonic()
        if not final and self.last is not None and tick - self.last < CADENCE_SECONDS:
            return False
        self.last = tick  # Failed I/O must not cause a hot retry loop.
        if final:
            self.closed = True
        try:
            if not _identifier(self.job.get('id')) or not _identifier(self.execution_id):
                return False
            attempt = self.attempt_reader()
            if type(attempt) is not int or attempt < 1:
                attempt = None
            value = {'schema_version': 1, 'job_id': self.job['id'], 'execution_id': self.execution_id,
                     'job_started_at': self.job['started_at'], 'reported_at': self.clock(), 'attempt': attempt}
            raw = recovery.json_bytes(value)
            if len(raw) > MAX_BYTES:
                return False
            with recovery.OwnedRoot(self.jobs_root, kind='heartbeat') as root:
                fd = recovery.directory_below(root, (self.job['id'],), 'heartbeat')
                try:
                    info = os.fstat(fd)
                    identity = (info.st_dev, info.st_ino)
                    if self.identity is not None and self.identity != identity:
                        return False
                    self.identity = identity
                    recovery.write_below(fd, 'heartbeat.json', raw, 'heartbeat')
                finally:
                    os.close(fd)
            return True
        except Exception:
            return False  # Observability failure is never a model-delivery failure.
