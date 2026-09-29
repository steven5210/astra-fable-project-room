"""Audited engineering-model transition core for one existing normal Project Room.

``audit`` performs AO GETs and local reads only: it writes no room file, no journal pointer and no state, and it
returns one deterministic evidence object whose SHA256 is the audit digest a transition must name. ``transition``
reobserves that exact evidence under the room lock, writes one create-once intent through
ao_engineering_model.store_once, publishes a pending journal pointer, rechecks the frozen conditions, writes a
durable attempt marker and then sends exactly one AO PATCH of model, reasoningEffort max and the preserved nonempty
approvalMode. It commits only when a fresh observation equals the intended target with an unchanged owner,
transcript, history, candidate, specification, routing, ledger and room state. ``abandon`` closes only the exact
pending request, and only when a fresh read-only observation still shows the recorded source settings and the otherwise unchanged frozen evidence; it sends no PATCH and makes no claim about an earlier attempt. Nothing here
edits the AO database, selects a replacement session or calls an internal vendor method.

Selector, epoch and expected-model logic stays in ao_engineering_model; this module composes with it and never
invents a second shape for the create-once journal.
"""

import copy
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
import xml.etree.ElementTree as ET

import ao_delegates
import ao_evidence_audit_native
import ao_engineering_model as em
import ao_native_identity
import ao_native_outcome
import ao_project_room as ao
import ao_routing
import ao_workflow
from room import RoomError

BASE = em.BASE
DIGEST = re.compile(r'[0-9a-f]{64}')
AGENT_TOOLS = frozenset(('Agent', 'Task'))
NOTIFICATION_FIELDS = ('task-id', 'tool-use-id', 'output-file', 'status', 'summary', 'note')
TERMINAL_NOTIFICATION_STATUS = frozenset(('completed', 'failed', 'cancelled', 'stopped', 'error'))
NO_PATCH = {'acknowledged': False, 'error': None, 'response_sha256': None}
MAX_TRANSCRIPT_BYTES = 64_000_000
AUDIT_MEANING = ('Current transition evidence only. The audit wrote nothing; a transition reobserves this exact '
                 'evidence under the room lock and writes one create-once intent. A configured-model change then '
                 'sends exactly one guarded AO conversation settings PATCH before it can commit, while a '
                 'same-selector qualification-only refresh sends zero PATCH. Qualification is not provider '
                 'availability.')
PENDING_MEANING = ('The engineering model transition is durably pending. Only the identical request can observe or '
                   'reconcile it, and abandon can close it; ordinary work stays blocked until then.')
ABANDON_MEANING = ('The exact pending request is closed as unchanged source. This records a fresh read-only '
                   'observation only: it makes no claim about whether the earlier PATCH attempt applied, and a '
                   'later transition needs a new request_id, a fresh audit and fresh authorization.')
LIMITATION = ('Child-internal work that never appears in the retained parent transcript cannot be observed. This '
              'proof claims only the parent Agent/Task launches and background task notifications that the complete '
              'transcript shows.')
COMPROMISED = ('Engineering model transition evidence is inconsistent with its own create-once journal; preserve and '
               'diagnose it before any further room change')
OBSERVED_SERVED = ('Not observed. A settings read-back or an acknowledged PATCH is configured AO state, not model '
                   'execution; only a later owned native response establishes the served model.')
QUALIFIED_TRANSITION = ('A new family transition or reset requires the operator-selected family qualification for that '
                        'family: an exact expected model established from retained source evidence before inference. '
                        'Configure the family_qualification artifact and audit again; nothing was written.')
QUALIFIED_RETAINED = ('The recorded qualified target retained artifact or source evidence is missing or changed; '
                      'restore the exact retained room bytes, this boundary never rewrites or repairs them from the '
                      'private originals')


class _NoWriteRefusal(RoomError):
    """A refusal that promises no room writes and must be raised after the room lock exits."""


class _ReadOnlyIdentity:
    """Service.identity without its single state-writing branch (record_reroute)."""

    identity = ao.Service.identity

    def __init__(self, service):
        self.root = service.root

    def record_reroute(self, directory, state, request, reroute):
        raise RoomError('Native model substitution contradicts the pinned engineering identity; the read-only '
                        'observation made no room change, diagnose it through ordinary sync')


def _stable_owner(owner):
    """Owner identity without the two liveness fields an abandonment may legitimately observe change."""
    return {key: value for key, value in owner.items() if key not in em.GENERATION_FIELDS}


def _state_with_pointer(state, pointer):
    """The exact room state with its transition head normalized to one recorded pointer."""
    value = copy.deepcopy(state)
    if pointer is None:
        value.pop(em.POINTER_KEY, None)
    else:
        value[em.POINTER_KEY] = copy.deepcopy(pointer)
    return value


def _state_digest(state):
    """Full room state digest, including the exact transition head this operation recorded."""
    return ao.digest(state)


def _before(evidence):
    """The audited observation exactly as ao_engineering_model reconstructs it for a record."""
    native = evidence['native']
    return {'settings': copy.deepcopy(native['settings']), 'controller': native['controller'],
            'history_sha256': native['history_sha256'], 'transcript_sha256': native['transcript_sha256'],
            'native_owner': copy.deepcopy(evidence['native_owner'])}


def _settings_path(binding):
    return '/sessions/' + binding['session_id'] + '/conversation/settings'


def _payload(target, settings):
    """The one settings payload: the target configured value at max, preserving a nonempty approvalMode."""
    payload = {'model': target['configured_model'], 'reasoningEffort': em.EFFORT}
    approval = settings.get('approvalMode')
    if isinstance(approval, str) and approval:
        payload['approvalMode'] = approval
    return payload


def _resolve_target(policy, target_model):
    """The qualified target selector and its immutable policy pin, resolved by ao_engineering_model itself."""
    if not isinstance(target_model, str) or not target_model.strip():
        raise RoomError('target_model must be the configured engineering value: a qualified family alias or an '
                        'exact identifier')
    selector = em.validate_selector(em._choose(policy, target_model))
    entry = em._entry_for(policy, selector)
    if entry is None:
        raise RoomError('The requested engineering target is not qualified for the engineer role; audit a qualified '
                        'selector')
    if em.configured_value(selector) != target_model:
        raise RoomError('The requested engineering target does not name its own configured value; audit it again')
    return {'selector': copy.deepcopy(selector), 'configured_model': em.configured_value(selector),
            'reasoning_effort': em.EFFORT, 'harness': em.HARNESS, 'qualification': copy.deepcopy(entry),
            'policy': copy.deepcopy(policy), 'policy_sha256': ao.digest(policy)}


def _target_execution(target):
    '''The target's own frozen family execution qualification, or None for an exact/unqualified target.'''
    entry = target.get('qualification') if isinstance(target, dict) else None
    execution = entry.get('execution_qualification') if isinstance(entry, dict) else None
    return execution if isinstance(execution, dict) else None


def _qualification_reference(snapshot):
    '''The compact room-relative reference one retained snapshot binds.'''
    return {key: copy.deepcopy(snapshot[key]) for key in ('sha256', 'record', 'evidence')}


def _qualification_snapshot(target, *, current):
    '''The retained-snapshot value of one target's pinned family qualification, or None.

    A newly selected target derives it with the active validator; a replay derives the same record
    and evidence fields from the frozen policy snapshot itself, so a later bundled-table change
    never reinterprets a preserved target and neither the frozen target nor its policy digest is
    ever edited.
    '''
    qualification = (target.get('policy') or {}).get('family_qualification')
    if not isinstance(qualification, dict):
        return None
    import ao_model_qualification as qmod
    if current:
        return qmod.snapshot(qualification)
    return {'artifact': copy.deepcopy(qualification['artifact']), 'sha256': qualification['sha256'],
            'record': qmod.record_path(qualification['sha256']),
            'evidence': qmod.evidence_entries(qualification['artifact'], qualification['sha256'])}


def _target_qualification(target, *, current):
    '''Cross-check one target's family expectation with its complete pinned policy snapshot.

    A newly selected family target requires its operator-selected qualification; a replay keeps the
    recorded contract of a historical unqualified family target. Every source-qualified family
    target must agree with its own policy snapshot's family mapping, expected model and
    qualification digest, and with ao_engineering_model's structural target context.
    '''
    context = em.qualification_context_for_target(target)
    if context.get('selector') != target['selector']:
        raise RoomError('The engineering model target does not name its own selector; audit the room again')
    if target['selector']['kind'] != 'family':
        return None
    execution = _target_execution(target)
    if execution is None:
        if current:
            raise RoomError(QUALIFIED_TRANSITION)
        return None
    policy_qualification = (target.get('policy') or {}).get('family_qualification')
    family = target['selector']['family']
    declared = None
    if isinstance(policy_qualification, dict):
        declared = (policy_qualification.get('artifact') or {}).get('families', {}).get(family)
    if (not isinstance(declared, dict) or policy_qualification.get('sha256') != execution['qualification_sha256']
            or declared.get('expected_model') != execution['expected_model']
            or context.get('expected_model') != execution['expected_model']):
        raise RoomError('The engineering model target family qualification does not match its own pinned policy '
                        'snapshot; audit the room again')
    snapshot = _qualification_snapshot(target, current=current)
    if context.get('reference') != _qualification_reference(snapshot):
        raise RoomError('The engineering model target qualification reference does not match its own pinned policy '
                        'snapshot; audit the room again')
    return snapshot


def _retained_record_present(directory, record):
    '''True when the room's qualification area already holds the named record; a symlink refuses later.'''
    path = Path(directory).joinpath(*Path(record).parts)
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RoomError(QUALIFIED_RETAINED) from exc
    return True


def _admit_qualified_target(service, directory, state, prepared, routing, target, snapshot, *, executable,
                            request_id, retained_only):
    '''Admit one target's own family qualification, and its workers' independent one, for this room.

    The lower explicit qmod helper is the only admission authority: it verifies the target artifact
    and every selected source capture (the room-retained proof when it exists, otherwise the selected
    private sources prospectively), applies the qualified compatibility floors to the concrete
    executable identity actually observed now, and checks the declared configuration context. The
    independently pinned worker snapshot, when the effective routing record carries one, is admitted
    as its own authority: a root artifact is never required to map a worker family. Its proof is
    located independently of the prospective root, because the versioned worker snapshot is a room-
    retained value: it is authenticated from its own room-retained artifact record and every one of
    its own selected captures even while a new prospective root target still authenticates its
    selected private sources. A replay uses the room-retained proof only; a missing or changed
    retained byte is never repaired from a private original, and an actual worker never loses its own
    proof.
    '''
    import ao_model_qualification as qmod
    worker = routing.get('worker_qualification') if isinstance(routing, dict) else None
    location = directory if retained_only or _retained_record_present(directory, snapshot['record']) else None
    configuration = {'config_dir': routing.get('claude_config_dir'), 'worktree': prepared['worktree'],
                     'agents': routing.get('agents'),
                     'foreground': ao_routing.foreground_configured(prepared['worktree'], routing),
                     'environ': os.environ,
                     'project_env': ao_routing._project_env(service.client(state), state)}
    admitted = qmod.admit_qualification(
        snapshot, target['selector']['family'], directory=location, worker_directory=directory,
        executable=executable, configuration=configuration,
        origin={'context': 'pending_engineering_target', 'request_id': request_id},
        worker_qualification=worker)
    return {'family': target['selector']['family'], 'expected_model': admitted['expected_model'],
            'sha256': admitted['qualification']['sha256'], 'record': admitted['qualification']['record'],
            'reference': _qualification_reference(snapshot), 'proof': admitted['proof'],
            'sources': copy.deepcopy(admitted['sources']),
            'minimum_claude_code_version': admitted['minimum_claude_code_version'],
            'effective_minimum_claude_code_version': admitted['effective_minimum_claude_code_version'],
            'worker_families': copy.deepcopy(admitted['worker_families']),
            'worker_expected_models': copy.deepcopy(admitted['worker_expected_models']),
            'worker_proof': admitted['worker_proof'],
            'worker_reference': copy.deepcopy(admitted['worker_reference']),
            'worker_sources': copy.deepcopy(admitted['worker_sources']),
            'executable': copy.deepcopy(admitted['executable'])}


def _retain_qualified_target(directory, target):
    '''Retain one target's qualified artifact and every selected capture before its intent.

    An existing retained record must verify completely first: a partial or damaged proof is never
    silently completed or repaired from the private originals, and only a genuinely absent record
    is written from the selected private sources. Repeating an identical retention crosses every
    durable barrier again from the room bytes.
    '''
    snapshot = _qualification_snapshot(target, current=True)
    if snapshot is None:
        return None
    import ao_model_qualification as qmod
    reference = _qualification_reference(snapshot)
    if _retained_record_present(directory, snapshot['record']):
        qmod.verify_reference(directory, reference, QUALIFIED_RETAINED)
    retained = qmod.retain(directory, snapshot)
    qmod.verify_reference(directory, reference, QUALIFIED_RETAINED)
    return retained


def _frozen_expectation(target):
    '''The pre-inference expectation one frozen target records, and the truthful basis for it.'''
    selector = target['selector']
    if selector['kind'] == 'exact':
        return {'expected_model': selector['model'], 'qualification_sha256': None,
                'expected_model_basis': 'exact selector identifier'}
    execution = _target_execution(target)
    if execution is None:
        return {'expected_model': None, 'qualification_sha256': None,
                'expected_model_basis': 'unqualified family epoch: no pre-inference expectation'}
    return {'expected_model': execution['expected_model'],
            'qualification_sha256': execution['qualification_sha256'],
            'expected_model_basis': 'operator-selected family qualification'}


def _require_qualification_unchanged(stored, fresh):
    '''The stable qualification authority and source identity must hold; only the proof may advance.

    An observation truthfully records whether it verified the selected private sources prospectively
    or the complete room-retained proof. Publication retains those exact sources, so a replay may
    observe the retained proof where the audit observed the private one: both are preserved exactly
    as observed and compared by stable authority and source identity, never by the proof label. Each
    other field is compared exactly.

    The primary root proof is always required, and a retained root proof is never downgraded to the
    private originals. An absent independent worker proof is admitted only for the genuinely absent
    and unchanged worker authority: the worker reference, sources and family context must then be
    absent too, and the exact comparison above already holds every other field equal. An actual
    independently qualified worker is never admitted without its own proof, and its own retained
    proof is never downgraded either.
    '''
    if not isinstance(stored, dict) or not isinstance(fresh, dict) or set(stored) != set(fresh):
        raise RoomError('The frozen engineering model transition qualification evidence changed shape; a fresh audit '
                        'is required')
    for key in sorted(stored):
        if key in ('proof', 'worker_proof'):
            continue
        if stored[key] != fresh[key]:
            raise RoomError('The frozen engineering model transition qualification evidence changed (' + key
                            + '); a fresh audit is required')
    before, after = stored.get('proof'), fresh.get('proof')
    if before not in ('private_prospective', 'retained') or after not in ('private_prospective', 'retained'):
        raise RoomError('The frozen engineering model transition qualification proof is unsupported; a fresh '
                        'audit is required')
    if before == 'retained' and after != 'retained':
        raise RoomError('The frozen engineering model transition retained qualification proof disappeared; a '
                        'fresh audit is required')
    worker_before, worker_after = stored.get('worker_proof'), fresh.get('worker_proof')
    if worker_before is None or worker_after is None:
        if worker_before is not None or worker_after is not None:
            raise RoomError('The frozen engineering model transition independent worker qualification proof '
                            'appeared or disappeared; a fresh audit is required')
        if (stored.get('worker_reference') is not None or fresh.get('worker_reference') is not None
                or stored.get('worker_sources') is not None or fresh.get('worker_sources') is not None
                or stored.get('worker_families') or fresh.get('worker_families')
                or stored.get('worker_expected_models') or fresh.get('worker_expected_models')):
            raise RoomError('The frozen engineering model transition independent worker qualification has '
                            'evidence without its own proof; a fresh audit is required')
        return
    if worker_before not in ('private_prospective', 'retained') or worker_after not in ('private_prospective',
                                                                                       'retained'):
        raise RoomError('The frozen engineering model transition independent worker qualification proof is '
                        'unsupported; a fresh audit is required')
    if worker_before == 'retained' and worker_after != 'retained':
        raise RoomError('The frozen engineering model transition retained independent worker qualification proof '
                        'disappeared; a fresh audit is required')


def _read_transcript(path_value, native_session_id):
    """Read the complete plain native transcript with the ownership rules the outcome lane already uses."""
    if not isinstance(path_value, str) or not path_value:
        raise RoomError('Supply the exact retained native transcript path')
    path = Path(path_value)
    if (not path.is_absolute() or path.name != native_session_id + '.jsonl'
            or any(part.is_symlink() for part in (path, *path.parents))):
        raise RoomError('Use the exact owned native transcript without symlinks; its file name must be the retained '
                        'provider session identity plus .jsonl')
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError as exc:
        raise RoomError('The retained native transcript is unavailable') from exc
    with os.fdopen(descriptor, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_mode & 0o022
                or before.st_size > MAX_TRANSCRIPT_BYTES):
            raise RoomError('Native transcript ownership, type or size is unsafe')
        raw = stream.read(MAX_TRANSCRIPT_BYTES + 1)
        after = os.fstat(stream.fileno())
    if (len(raw) != before.st_size or (before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_ino, after.st_size, after.st_mtime_ns)):
        raise RoomError('Native transcript changed while it was being observed')
    return raw


def _notification_fields(text):
    """One strict task-notification envelope; anything else is unknown child evidence."""
    if (not re.match(r'<task-notification>\r?\n', text) or not re.search(r'\r?\n</task-notification>$', text)
            or '<!' in text or '<?' in text):
        raise RoomError('A task notification in the retained transcript has an unknown envelope; child evidence is '
                        'unknown')
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise RoomError('A task notification in the retained transcript is malformed; child evidence is unknown') from exc
    if (root.tag != 'task-notification' or root.attrib or root.text != '\n' or root.tail
            or tuple(child.tag for child in root) != NOTIFICATION_FIELDS
            or any(child.attrib or len(child) or not isinstance(child.text, str) or not child.text
                   or child.tail != '\n' for child in root)):
        raise RoomError('A task notification in the retained transcript has an unknown shape; child evidence is '
                        'unknown')
    return {child.tag: child.text for child in root}


def _sdk_notification(row, session_id, workspace):
    """A genuine native/SDK terminal task notification, not arbitrary XML or prose."""
    failed = ao_native_outcome._failed_task_notification(row, session_id, workspace)
    if failed is not None:
        return _notification_fields(failed)
    if (row.get('type') != 'user' or row.get('sessionId') != session_id or row.get('cwd') != workspace
            or row.get('isSidechain') is not False or row.get('origin') != {'kind': 'task-notification'}
            or row.get('promptSource') != 'sdk' or row.get('queueSkipAttachments') is not True
            or row.get('userType') != 'external' or row.get('entrypoint') != 'sdk-ts'
            or any(key in row for key in ('isCompactSummary', 'isVisibleInTranscriptOnly'))
            or any(row.get(key) is not None for key in ('agentId', 'agent_id'))):
        return None
    message = row.get('message')
    content = message.get('content') if isinstance(message, dict) and message.get('role') == 'user' else None
    if not isinstance(content, str):
        return None
    fields = _notification_fields(content)
    if fields['status'] not in TERMINAL_NOTIFICATION_STATUS:
        raise RoomError('A task notification reports an unknown nonterminal status; child evidence is unknown')
    return fields


def _structured_identity_field(value, key):
    """One explicitly present structured identity field, or None when the field is genuinely absent."""
    if key not in value:
        return None
    item = value[key]
    if not isinstance(item, str) or not item or not ao_evidence_audit_native.bounded_text(item):
        raise RoomError('A structured child result reports a malformed ' + key
                        + ' identity; child evidence is unknown')
    return item


def _structured_result_identity(value):
    """The supported structured agent/task identity of one toolUseResult projection, or None.

    An explicit task identity is one binding, never two: a result that reports both taskId and
    task_id must report the same value, and no alias is invented between an agent id and a task id.
    A field that is explicitly present but malformed is never treated as absent.
    """
    if not isinstance(value, dict):
        return None
    agent = None
    if 'agentId' in value:
        agent = ao_evidence_audit_native.structured_agent_id(value)
        if agent is None:
            raise RoomError('A structured child result reports a malformed agent identity; child evidence is unknown')
    task = _structured_identity_field(value, 'taskId')
    alias = _structured_identity_field(value, 'task_id')
    if task is not None and alias is not None and task != alias:
        raise RoomError('A structured child result reports conflicting task identities; child evidence is unknown')
    if agent is None and task is None and alias is None:
        return None
    return {'agent_id': agent, 'task_id': task if task is not None else alias}


FAILED_LAUNCH_STATUS = frozenset(('failed', 'cancelled', 'stopped', 'error'))
AGENT_API_ERROR_PREFIX = 'Agent terminated early due to an API error: '
AGENT_API_ERROR_RESULT_PREFIX = 'Error: '


def _failed_launch_projection(value, launch):
    """A positively correlated supported failed-launch projection, or None for unknown evidence."""
    if not isinstance(value, dict):
        return None
    status = value.get('status')
    if status not in FAILED_LAUNCH_STATUS:
        return None
    identity = _structured_result_identity(value)
    if identity is None:
        return None
    prompt = value.get('prompt')
    if prompt is not None and prompt != launch['prompt']:
        return None
    return {'status': status, 'identity': identity, 'prompt': prompt}


def _terminal_api_error(item, launch):
    """The one supported native foreground Agent API-error termination projection, or None.

    The installed producer throws its typed termination error only after the child stream ended with a
    final API error; block.content carries the typed message prefix and details, and the row-level
    toolUseResult is exactly the Error-prefixed form of that same content. Only that exact relation is
    supported: an attempted child model text is never a served identity, a bare error never proves
    termination, and no availability, reset or retry inference follows.
    """
    if launch['background'] or item['classification'] != 'error' or item['shared_projection']:
        return None
    if item['source_uuid'] != launch['row_uuid']:
        return None
    content, structured = item['result_content'], item['tool_use_result']
    if not isinstance(content, str) or not isinstance(structured, str):
        return None
    if not content.startswith(AGENT_API_ERROR_PREFIX):
        return None
    if structured != AGENT_API_ERROR_RESULT_PREFIX + content:
        return None
    detail = content[len(AGENT_API_ERROR_PREFIX):]
    if not detail.strip():
        return None
    return {'kind': 'terminal_api_error', 'error_kind': 'AgentApiErrorTerminationError',
            'error_text_sha256': ao.digest(content.encode()), 'detail_sha256': ao.digest(detail.encode())}


def _launch_projection(item, launch):
    """The complete supported lifecycle projection of one result observed for a proven launch."""
    value = item['tool_use_result']
    completion = ao_evidence_audit_native.completion_observation(value, item['canonical'], item['timestamp'])
    failed = _failed_launch_projection(value, launch)
    identity = _structured_result_identity(value)
    status = value.get('status') if isinstance(value, dict) and isinstance(value.get('status'), str) else None
    return {'canonical': item['canonical'], 'classification': item['classification'], 'identity': identity,
            'status': status,
            'completion': None if completion is None else {'agent_id': completion[0], 'fingerprint': completion[1],
                                                           'prompt': completion[2]},
            'failed': failed, 'api_error': _terminal_api_error(item, launch)}


def child_evidence(raw, native_session_id, workspace):
    """Independently prove the parent Agent/Task launches and background notifications of the plain transcript.

    Every parent row must prove the retained session and worktree. Child-internal rows are never claimed. A launch
    without a correlated owned result, a foreground launch without a completed projection or failed-launch result,
    a background launch without its exact terminal SDK notification, a notification naming no recorded parent
    launch, and any ambiguous, torn or conflicting duplicate all refuse instead of being assumed successful.
    """
    if not isinstance(raw, bytes):
        raise RoomError('The retained native transcript is unreadable')
    if not isinstance(native_session_id, str) or not native_session_id:
        raise RoomError('The retained native session identity is unavailable; child evidence is unknown')
    if not isinstance(workspace, str) or not workspace:
        raise RoomError('The retained native workspace is unavailable; child evidence is unknown')
    launches, tools, results, notifications, seen_rows = {}, {}, {}, {}, {}
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise RoomError('The retained native transcript contains a malformed row; child evidence is unknown') from exc
        if not isinstance(row, dict):
            raise RoomError('The retained native transcript contains a malformed row; child evidence is unknown')
        identity = row.get('uuid')
        if isinstance(identity, str) and identity:
            stamp = ao.digest(row)
            if identity in seen_rows:
                if seen_rows[identity] != stamp:
                    raise RoomError('Conflicting duplicate native records refuse child evidence')
                continue
            seen_rows[identity] = stamp
        sidechain = row.get('isSidechain')
        agent = row.get('agentId') or row.get('agent_id')
        message = row.get('message')
        if sidechain is True:
            if not isinstance(agent, str) or not agent:
                raise RoomError('A sidechain row lacks its child identity; child evidence is unknown')
            continue
        if agent is not None:
            raise RoomError('A row with an agent identity is neither a proven child row nor a parent row; child '
                            'evidence is unknown')
        if not isinstance(message, dict):
            continue
        if row.get('sessionId') != native_session_id or row.get('cwd') != workspace:
            raise RoomError('A transcript row does not belong to the retained parent session/workspace; child '
                            'evidence is unknown')
        kind = row.get('type')
        content = message.get('content')
        if kind == 'assistant':
            if message.get('role') != 'assistant' or not isinstance(content, list):
                raise RoomError('A parent assistant row has an unsupported shape; child evidence is unknown')
            for block in content:
                if not isinstance(block, dict):
                    raise RoomError('A parent content block is malformed; child evidence is unknown')
                if block.get('type') != 'tool_use':
                    continue
                tool_id = block.get('id')
                name = block.get('name')
                if not isinstance(tool_id, str) or not tool_id or not isinstance(name, str) or not name:
                    raise RoomError('A parent tool launch lacks its tool-use identity; child evidence is unknown')
                canonical = ao.digest({'type': 'tool_use', 'id': tool_id, 'name': name,
                                       'input': block.get('input')})
                if tool_id in tools:
                    if tools[tool_id] != canonical:
                        raise RoomError('Conflicting duplicate parent tool-use identities refuse child evidence')
                    continue
                tools[tool_id] = canonical
                if name not in AGENT_TOOLS:
                    continue
                inputs = block.get('input')
                if not isinstance(inputs, dict):
                    raise RoomError('A parent Agent/Task launch has no readable input; child evidence is unknown')
                prompt = inputs.get('prompt')
                if not isinstance(prompt, str) or not prompt:
                    raise RoomError('A parent Agent/Task launch has no retained prompt; child evidence is unknown')
                background = inputs.get('run_in_background', False)
                if type(background) is not bool:
                    raise RoomError('A parent Agent/Task launch has an unknown background flag; child evidence is '
                                    'unknown')
                launches[tool_id] = {'row_uuid': row.get('uuid'), 'name': name, 'background': background,
                                     'prompt': prompt, 'canonical': canonical}
        elif kind == 'user':
            if message.get('role') != 'user':
                raise RoomError('A parent user row has an unsupported role; child evidence is unknown')
            if isinstance(content, str):
                if content.lstrip().startswith('<task-notification>'):
                    fields = _sdk_notification(row, native_session_id, workspace)
                    if fields is None:
                        raise RoomError('A task notification in the retained transcript lacks SDK provenance; child '
                                        'evidence is unknown')
                    notifications.setdefault(fields['tool-use-id'], []).append((row.get('uuid'), fields))
                continue
            if not isinstance(content, list):
                raise RoomError('A parent user row has an unsupported content shape; child evidence is unknown')
            result_blocks = sum(1 for block in content
                                if isinstance(block, dict) and block.get('type') == 'tool_result')
            for block in content:
                if not isinstance(block, dict):
                    raise RoomError('A parent user content block is malformed; child evidence is unknown')
                if block.get('type') == 'tool_result':
                    origin = row.get('origin')
                    if isinstance(origin, dict) and origin.get('kind') == 'human':
                        raise RoomError('A human-authored tool result cannot establish child execution; child evidence '
                                        'is unknown')
                    tool_id = block.get('tool_use_id')
                    source_uuid = row.get('sourceToolAssistantUUID')
                    if not isinstance(tool_id, str) or not tool_id:
                        raise RoomError('A parent tool result lacks its tool-use identity; child evidence is unknown')
                    if not isinstance(source_uuid, str) or not source_uuid:
                        raise RoomError('A parent tool result lacks its source assistant identity; child evidence is '
                                        'unknown')
                    results.setdefault(tool_id, []).append({
                        'row_uuid': row.get('uuid'), 'source_uuid': source_uuid,
                        'canonical': ao_evidence_audit_native.canonical_result_block(block),
                        'classification': ao_evidence_audit_native.result_classification(block),
                        'result_content': block.get('content') if isinstance(block.get('content'), str) else None,
                        'tool_use_result': row.get('toolUseResult'),
                        'shared_projection': result_blocks != 1 and row.get('toolUseResult') is not None,
                        'timestamp': ao_evidence_audit_native.parse_timestamp(row.get('timestamp'))})
                elif block.get('type') == 'text' and isinstance(block.get('text'), str) and block['text'].lstrip().startswith('<task-notification>'):
                    raise RoomError('A task notification in a list content block is unsupported; child evidence is '
                                    'unknown')
        else:
            raise RoomError('A parent transcript row has an unsupported type; child evidence is unknown')
    terminal_api_errors = 0
    identity_owners = {}
    for tool_id, launch in launches.items():
        observations = results.get(tool_id, [])
        if not observations:
            raise RoomError('A parent Agent/Task launch in the retained transcript has no correlated result; an '
                            'unfinished child refuses the transition')
        if any(item['source_uuid'] != launch['row_uuid'] for item in observations):
            raise RoomError('A parent tool result is not owned by the exact launching message; child evidence is '
                            'unknown')
        if any(item['canonical'] is None or item['classification'] is None for item in observations):
            raise RoomError('A proven parent Agent/Task launch has an unsupported tool result shape; child evidence is unknown')
        if any(item['shared_projection'] for item in observations):
            raise RoomError('A row-level structured tool result is shared by more than one result and cannot '
                            'authorize an Agent/Task launch; child evidence is unknown')
        projections = {}
        for item in observations:
            projection = _launch_projection(item, launch)
            authorities = [value for value in (projection['completion'], projection['failed'],
                                               projection['api_error']) if value is not None]
            if len(authorities) > 1:
                raise RoomError('A parent Agent/Task result reports conflicting terminal authorities; child evidence is unknown')
            if projection['completion'] is not None and projection['classification'] != 'non_error':
                raise RoomError('A parent Agent/Task result reports completion with a non-success classification; child evidence is unknown')
            if projection['failed'] is not None and projection['classification'] != 'error':
                raise RoomError('A parent Agent/Task result reports a structured failed launch with a non-error classification; child evidence is unknown')
            projections[ao.digest(projection)] = projection
        if len(projections) != 1:
            raise RoomError('Conflicting duplicate native tool result lifecycle projections refuse child evidence')
        projection = next(iter(projections.values()))
        identity = projection['identity'] or {}
        for kind, value in (('agent', identity.get('agent_id')),
                            ('agent', None if projection['completion'] is None
                             else projection['completion']['agent_id']),
                            ('task', identity.get('task_id'))):
            if not isinstance(value, str) or not value:
                continue
            owner = identity_owners.get((kind, value))
            if owner is not None and owner != tool_id:
                raise RoomError('A structured child identity is reused by a different fresh Agent/Task launch; '
                                'child evidence is unknown')
            identity_owners[(kind, value)] = tool_id
        notification = notifications.get(tool_id)
        if launch['background']:
            if not notification:
                raise RoomError('A parent background Agent/Task launch has no terminal task notification; an '
                                'unfinished or unreported child refuses the transition')
            fields_list = [fields for _, fields in notification]
            if len({ao.digest(fields) for fields in fields_list}) != 1:
                raise RoomError('Conflicting terminal task notifications refuse child evidence')
            fields = fields_list[0]
            identity = projection['identity'] or {}
            task_id = identity.get('task_id')
            if task_id is not None:
                # An explicit task identity is the task binding; an agent id is never an alias for it.
                if fields['task-id'] != task_id:
                    raise RoomError('A terminal task notification names a different task identity than the explicit '
                                    'task identity of its launch; child evidence is unknown')
                continue
            agents = {value for value in (identity.get('agent_id'), None if projection['completion'] is None
                      else projection['completion']['agent_id']) if isinstance(value, str) and value}
            if not agents:
                raise RoomError('A background Agent/Task launch has no structured agent/task identity to bind its '
                                'terminal notification; child evidence is unknown')
            if fields['task-id'] not in agents:
                raise RoomError('A terminal task notification does not name the structured child agent identity of '
                                'its launch; child evidence is unknown')
            continue
        if notification:
            raise RoomError('A task notification names a non-background parent launch; child evidence is unknown')
        if projection['api_error'] is not None:
            terminal_api_errors += 1
            continue
        if projection['failed'] is not None:
            continue
        if projection['completion'] is not None and projection['completion']['prompt'] == launch['prompt']:
            continue
        if projection['classification'] == 'non_error':
            raise RoomError('A foreground Agent/Task launch has only a start acknowledgement; a validated completion '
                            'or proven terminal failure is required')
        raise RoomError('A parent Agent/Task launch has no validated terminal result; child evidence is unknown')
    for tool_id in notifications:
        if tool_id not in launches:
            raise RoomError('A task notification names no recorded parent Agent/Task launch; child evidence is '
                            'unknown')
    return {'parent_tool_uses': len(tools), 'agent_launches': len(launches),
            'background_launches': sum(1 for item in launches.values() if item['background']),
            'correlated_results': len(set(launches) & set(results)),
            'terminal_notifications': len(notifications), 'terminal_api_errors': terminal_api_errors,
            'launches_sha256': ao.digest([{'id': tool_id,
                                           **{key: launches[tool_id][key] for key in ('name', 'background', 'row_uuid')}}
                                          for tool_id in sorted(launches)]),
            'limitation': LIMITATION}


def history_digest(turns, messages):
    """The native history identity: turns and messages only, so a settings change never moves it."""
    return ao.digest({'turns': sorted(turns, key=lambda item: item['id']),
                      'messages': sorted(messages, key=lambda item: item['id'])})


def _turn_projection(turn):
    if not isinstance(turn, dict):
        raise RoomError('A retained native turn is malformed; history cannot be attributed')
    for key in ('id', 'state', 'providerTurnId'):
        if not isinstance(turn.get(key), str) or not turn[key]:
            raise RoomError('A retained native turn lacks its supported immutable projection; history cannot be '
                            'attributed')
    return {key: turn[key] for key in ('id', 'state', 'providerTurnId')}


def _message_projection(message):
    if not isinstance(message, dict):
        raise RoomError('A retained native message is malformed; history cannot be attributed')
    for key in ('id', 'turnId', 'role'):
        if not isinstance(message.get(key), str) or not message[key]:
            raise RoomError('A retained native message lacks its supported immutable projection; history cannot be '
                            'attributed')
    text = message.get('text')
    if text is not None and not isinstance(text, str):
        raise RoomError('A retained native message text is malformed; history cannot be attributed')
    return {'id': message['id'], 'turnId': message['turnId'], 'role': message['role'], 'text': text}


def _message_projection_set(messages):
    return sorted((_message_projection(message) for message in messages), key=lambda item: item['id'])


def _require_complete_history_proof(directory, request):
    """A truncated receipt is admissible only under the proof that covers its current invalidation head.

    reconcile legitimately renews a proof whose recorded invalidation is the validated head in force; an
    older invalidation stays in the journal history. The exact proof/head pair is compared, never the mere
    presence of an invalidation pointer, and load_proof is never a normalization authority.
    """
    from ao_history_reconciliation import invalidation_head, load_proof
    proof = load_proof(directory, request)
    if proof is None or proof.get('invalidation_sha256') != invalidation_head(directory, request):
        raise RoomError('An owning native request was captured with truncated history and has no current '
                        'complete-history reconciliation proof; its owned output cannot be attributed')


def _native_history(directory, state, binding, snapshot):
    """Complete AO history with owning receipts, settled-failure provenance and proven context imports."""
    if snapshot.get('history_truncated') is not False:
        raise RoomError('Complete native history is required; truncated or unknown AO history refuses')
    turns, messages = snapshot.get('turns'), snapshot.get('messages')
    if (not isinstance(turns, list) or not isinstance(messages, list)
            or any(not isinstance(item, dict) for item in turns + messages)):
        raise RoomError('AO must report explicit native turn and message arrays; history cannot be attributed')
    ids = [turn.get('id') for turn in turns]
    message_ids = [message.get('id') for message in messages]
    if (any(not isinstance(value, str) or not value for value in ids + message_ids)
            or len(set(ids)) != len(ids) or len(set(message_ids)) != len(message_ids)):
        raise RoomError('Native turn or message identities are ambiguous; history cannot be attributed')
    from ao_outcomes import validate_settlement
    owned, settled = {}, {}
    for request in (state.get('requests') or {}).values():
        if request.get('session_id') != binding['session_id']:
            continue
        turn_id = request.get('turn_id')
        if request.get('model_reroute'):
            raise RoomError('A native model substitution contradicts the pinned engineering identity')
        if request.get('state') == 'settled_failure':
            validate_settlement(directory, request)
            settled[turn_id] = request
        elif (request.get('state') != 'completed' or not isinstance(turn_id, str) or not turn_id
              or not ao.native_turn_identity(request)):
            raise RoomError('Every owning native request must be known completed with native identity; unsettled work '
                            'refuses')
        if turn_id in owned:
            raise RoomError('Two owning requests claim the same native turn')
        owned[turn_id] = request
        receipt = ao_workflow.completed_receipt(directory, request)
        if receipt.get('history_truncated') is not False:
            _require_complete_history_proof(directory, request)
        turn = receipt.get('turn') if isinstance(receipt, dict) else None
        if (not isinstance(turn, dict) or turn.get('id') != turn_id
                or turn.get('state') != ('failed' if request.get('state') == 'settled_failure' else 'completed')
                or turn.get('providerTurnId') != ao.native_turn_identity(request)
                or ao.digest(str(request.get('text', '')).encode()) != request.get('text_sha256')
                or not ao.sent_message(request, receipt) or not ao.sent_message(request, snapshot)):
            raise RoomError('An owning native request differs from its immutable receipt or observed message')
        live_turn = next((item for item in turns if item.get('id') == turn_id), None)
        if live_turn is None or _turn_projection(live_turn) != _turn_projection(turn):
            raise RoomError('The observed native turn differs from the immutable receipt for this owned request')
        if (_message_projection_set([item for item in messages if item.get('turnId') == turn_id])
                != _message_projection_set([item for item in (receipt.get('messages') or [])
                                            if item.get('turnId') == turn_id])):
            raise RoomError('Observed native messages differ from the immutable receipt for this owned request')
    if set(owned) - set(ids):
        raise RoomError('Owned native turns are missing from the observed history')
    for turn in turns:
        native_id = turn.get('providerTurnId')
        if not isinstance(native_id, str) or not native_id.strip():
            raise RoomError('A completed native turn lacks its provider identity')
        if turn['id'] in owned and native_id != ao.native_turn_identity(owned[turn['id']]):
            raise RoomError('An observed native provider identity differs from the saved request')
    from ao_outcomes import known_context_turns
    imports = known_context_turns(directory, state, snapshot)
    for turn in turns:
        if turn.get('state') != 'completed' and turn['id'] not in settled and turn['id'] not in imports:
            raise RoomError('Native history contains a turn that is not completed (state ' + str(turn.get('state'))
                            + '); failed, interrupted, cancelled, recovered or active work refuses')
    unowned = [turn['id'] for turn in sorted(turns, key=lambda item: item['id'])
               if turn['id'] not in owned and turn['id'] not in imports]
    if unowned:
        raise RoomError('Native history contains a turn that no retained request owns and no audited context proof '
                        'covers; an unowned or intervening turn refuses the transition')
    return {'turn_count': len(turns), 'message_count': len(messages),
            'owned_turns': [{'turn_id': key, 'provider_turn_id': ao.native_turn_identity(value),
                             'request_id': value['request_id']} for key, value in sorted(owned.items())],
            'context_imports': sorted(imports), 'history_sha256': history_digest(turns, messages)}


def _inventory(state):
    """Every request that existed at this audit, sorted, with orders exactly 1..N."""
    items = []
    for request in (state.get('requests') or {}).values():
        if not isinstance(request, dict):
            raise RoomError('Retained request records are malformed; preserve and diagnose')
        items.append({'request_id': request.get('request_id'), 'created_order': request.get('created_order')})
    items.sort(key=lambda item: item['created_order'] if type(item['created_order']) is int else 0)
    try:
        em._inventory(items)
    except RoomError as exc:
        raise RoomError('Retained request creation order is missing, duplicated, non-integer or out of range; '
                        'preserve and diagnose') from exc
    return items


def _require_known_outcomes(directory, state, session_id):
    """The latest retained engineer semantic outcome must be known; a hold is retained, never cleared here."""
    rows = [request for request in (state.get('requests') or {}).values() if request.get('session_id') == session_id]
    if not rows:
        return
    latest = max(rows, key=lambda request: request.get('created_order')
                 if type(request.get('created_order')) is int else 0)
    from ao_outcomes import classify, inconclusive, load, receipt
    record = load(directory, latest) if latest.get('semantic_outcome') else None
    if record is not None:
        value = record.get('outcome') or {}
        if value.get('kind') == 'unknown' or inconclusive(record):
            raise RoomError('The latest engineering semantic outcome is unknown; audit its exact native outcome before '
                            'the transition')
        return
    value = classify(receipt(directory, latest), None, require_structured=True)
    if value.get('kind') == 'unknown':
        raise RoomError('The latest engineering semantic outcome is unknown (' + str(value.get('reason'))[:200]
                        + '); audit it before the transition')


def _observe(service, directory, state, binding, expected, owner_database, transcript_path, worktree):
    """One read-only observation: AO identity, complete history, the retained owner and the plain transcript."""
    snapshot = _ReadOnlyIdentity(service).identity(service.client(state), state, binding, expected=expected)
    if snapshot.get('history_truncated') is not False:
        raise RoomError('Complete native history is required; truncated or unknown AO history refuses')
    history = _native_history(directory, state, binding, snapshot)
    try:
        owner = ao_native_identity.read_owner(owner_database, binding['session_id'])
    except (ValueError, OSError) as exc:
        raise RoomError('The retained native owner evidence is unavailable or inconsistent: ' + str(exc)[:300]) from exc
    if owner['project_id'] != state.get('ao_project_id'):
        raise RoomError('The retained native owner evidence belongs to another AO project')
    if owner['workspace_path'] != worktree:
        raise RoomError('The retained native owner evidence workspace differs from the prepared engineer worktree')
    if owner['ao_conversation_id'] != binding['conversation_id'] or owner['active_branch_id'] != binding['branch_id']:
        raise RoomError('The retained native owner evidence conversation or branch differs from the bound engineer')
    raw = _read_transcript(transcript_path, owner['provider_conversation_id'])
    children = child_evidence(raw, owner['provider_conversation_id'], worktree)
    return ({'settings': copy.deepcopy(snapshot.get('settings') or {}), 'controller': snapshot.get('controller'),
             'history_sha256': history['history_sha256'], 'transcript_sha256': ao.digest(raw),
             'native_owner': copy.deepcopy(owner)},
            {'snapshot': snapshot, 'history': history, 'children': children, 'owner': owner})


def _inspect(service, directory, state, target_model, owner_database, transcript_path, expect_target, allow_pending,
             target_override=None, allow_stopped_controller=False, allow_exited_owner=False, pending_request_id=None):
    """Build the exact frozen transition evidence. Read-only: AO GETs and local reads only."""
    if not ao_workflow.normal(state):
        raise RoomError('The audited engineering model transition applies to normal Fable-engineering rooms only')
    service.settled(state, pending_model_transition=allow_pending)
    from ao_review_extension import guard_replacement
    guard_replacement(state, 'engineering model transition')
    import ao_acceptance_extension
    ao_acceptance_extension.guard_unused(service, state, 'engineering model transition')
    chain = em.journal(directory, state, allow_pending=allow_pending)
    if chain.get('pending_resolution') is not None:
        raise RoomError('A validated historical engineering model resolution exists on disk without its journal '
                        'pointer; reconcile the identical adoption before the engineering model transition')
    state_for_evidence = state
    if allow_pending and chain.get('pending') is not None:
        if pending_request_id is not None and chain['pending']['request_id'] != pending_request_id:
            raise RoomError('A different engineering model transition is pending ('
                            + chain['pending']['request_id'] + '); only that exact request can observe or reconcile it')
        state_for_evidence = _state_with_pointer(state, chain['pending']['intent_value']['previous'])
    engineer = (state.get('bindings') or {}).get('engineer')
    if not engineer:
        raise RoomError('Bind the prepared native engineering orchestrator first')
    for key in ('session_id', 'conversation_id', 'branch_id'):
        if not isinstance(engineer.get(key), str) or not engineer[key]:
            raise RoomError('The retained engineer binding lacks its exact session, conversation or branch identity')
    epoch = em.current(directory, state)
    source = {'selector': copy.deepcopy(epoch['selector']), 'configured_model': epoch['configured_model'],
              'reasoning_effort': epoch['reasoning_effort']}
    if target_override is None:
        policy = em.effective_policy(service.root)[0]
        target = _resolve_target(policy, target_model)
    else:
        target = copy.deepcopy(target_override)
        em._target(target)
    if target['configured_model'] != target_model:
        raise RoomError('The engineering model target does not name its own configured value; audit the room again')
    qualification_snapshot = _target_qualification(target, current=target_override is None)
    reset = target['configured_model'] == source['configured_model']
    if reset and (target['selector'] != source['selector'] or target['selector']['kind'] != 'family'):
        raise RoomError('A target equal to the configured value in force is only the identical family selector reset, '
                        'which opens a new family epoch without a PATCH; choose a different qualified selector')
    spec = service.spec(directory, state)
    prepared = ao_delegates.validate_preparation(directory, state, engineer['session_id'], check_routing=False)
    routing = ao_routing.validate_local(prepared, state, directory)
    if not isinstance(routing, dict) or type(routing.get('version')) is not int or routing['version'] not in (1, 2, 3):
        raise RoomError('The engineering model transition requires this room to hold a validated current effective '
                        'native routing preparation')
    observed_rules = ao_routing.observe_rules(service.client(state), state)
    if not ao_routing.rules_match(observed_rules, routing.get('rules')):
        raise RoomError('AO project routing rules changed; reconcile the project rules before the transition')
    if 'compaction' in routing:
        ao_routing._compaction_sources(routing['compaction'], routing['claude_config_dir'],
                                       Path(prepared['worktree']), os.environ)
        ao_routing._compaction_project(service.client(state), state, routing['compaction'])
    executable = em.executable_identity(directory, state, prepared) or {}
    em.require_executable(directory, state, prepared, target['configured_model'], target['qualification'])
    executable_evidence = {'path': executable.get('path'), 'version': executable.get('version'),
                           'error': executable.get('error')}
    qualification_admission = None
    if qualification_snapshot is not None:
        # A qualified root transition re-probes the effective pinned or audited-bound path exactly as
        # the reviewed routing boundary does: the recorded value is not evidence of what that path
        # reports now. The bounded identity actually observed is what the qualified floors apply to
        # and what this observation freezes; no controller lifecycle is touched here.
        import ao_executable_binding
        import ao_routing_refresh
        replacement = ao_executable_binding.effective(directory, state, prepared)
        try:
            fresh_executable = ao_routing_refresh._current_executable(service, directory, state, routing,
                                                                       replacement, None)
        except RoomError as exc:
            raise RoomError('The qualified engineering target requires the Claude executable identity actually in '
                            'force now: ' + str(exc)[:400]) from exc
        qualification_admission = _admit_qualified_target(
            service, directory, state, prepared, routing, target, qualification_snapshot,
            executable=fresh_executable, request_id=pending_request_id,
            retained_only=target_override is not None)
        executable_evidence = copy.deepcopy(fresh_executable)
    actual = ao_workflow.workspace(service, directory, state, check_routing=False)
    candidate = ao.candidate_snapshot(actual)
    ao_delegates.assert_settled(service.root.parent, copy.deepcopy(state), directory)
    from ao_provider_transition import _ledger_check, _ledger_rows
    ledger_present, ledger_rows = _ledger_rows(service.root.parent, state['room_id'])
    _ledger_check(ledger_rows)
    _require_known_outcomes(directory, state, engineer['session_id'])
    observation, extras = _observe(
        service, directory, state, engineer,
        {'model': target['configured_model'], 'reasoning_effort': em.EFFORT} if expect_target else None,
        owner_database, transcript_path, prepared['worktree'])
    if observation['controller'] != 'ready' and not (allow_stopped_controller and observation['controller'] == 'stopped'):
        raise RoomError('Installed AO changes conversation settings only for a live idle controller; the retained '
                        'engineer controller reports ' + str(observation['controller']))
    if (not expect_target and not allow_exited_owner
            and observation['native_owner']['activity_state'] != 'idle'):
        raise RoomError('The retained native owner must be idle for the audit; activity '
                        + str(observation['native_owner']['activity_state']) + ' refuses')
    evidence = {
        'version': 1, 'room_id': state['room_id'], 'state_sha256': _state_digest(state_for_evidence), 'source': source,
        'target': {key: copy.deepcopy(target[key])
                   for key in ('selector', 'configured_model', 'qualification', 'policy_sha256')},
        'engineer': copy.deepcopy(engineer), 'requests': _inventory(state),
        'spec_record_sha256': state.get('spec_record_sha256'),
        'candidate': {'sha256': candidate['sha256'], 'head': candidate['head']},
        'native': {'session_id': engineer['session_id'], 'conversation_id': engineer['conversation_id'],
                   'branch_id': engineer['branch_id'], 'controller': observation['controller'],
                   'settings': copy.deepcopy(observation['settings']),
                   'history_sha256': observation['history_sha256'],
                   'transcript_sha256': observation['transcript_sha256']},
        'native_owner': copy.deepcopy(observation['native_owner']),
        'journals': {'provider_transition': copy.deepcopy(state.get('provider_transition')),
                     'executable_binding': copy.deepcopy(state.get('executable_binding')),
                     'routing_refresh': copy.deepcopy(state.get('routing_refresh')),
                     'routing_adoption': copy.deepcopy(state.get('routing_adoption')),
                     'spec_review_extension': copy.deepcopy(state.get('spec_review_extension')),
                     'acceptance_review_extension': copy.deepcopy(state.get('acceptance_review_extension')),
                     'resolutions': copy.deepcopy(state.get(em.RESOLUTIONS_KEY))},
        'room': {'spec': state.get('spec'), 'preparation': state.get('preparation'),
                 'preparation_sha256': state.get('preparation_sha256'), 'handoff': state.get('handoff'),
                 'handoff_sha256': state.get('handoff_sha256'), 'checkpoint': state.get('checkpoint'),
                 'checkpoint_sha256': state.get('checkpoint_sha256'),
                 'delegate': copy.deepcopy(state.get('delegate'))},
        'spec': {'revision': spec['revision'], 'sha256': spec['sha256'],
                 'record_sha256': state['spec_record_sha256']},
        'preparation': {'path': state.get('preparation'), 'sha256': state.get('preparation_sha256'),
                        'worktree': prepared['worktree'], 'provider': prepared.get('provider')},
        'routing': copy.deepcopy(routing), 'rules': copy.deepcopy(observed_rules),
        'executable': executable_evidence,
        **({} if qualification_admission is None else {'qualification': copy.deepcopy(qualification_admission)}),
        'ledger': {'present': ledger_present, 'count': len(ledger_rows), 'sha256': ao.digest(ledger_rows)},
        'history': {key: extras['history'][key]
                    for key in ('turn_count', 'message_count', 'owned_turns', 'context_imports')},
        'children': extras['children'], 'reset': reset}
    return evidence, target


def _pointers(directory, state):
    """Read-only walk of the published transition pointers, latest first."""
    result, seen, pointer = [], set(), state.get(em.POINTER_KEY)
    while pointer is not None:
        try:
            pointer = em._pointer(pointer)
        except RoomError as exc:
            raise RoomError(COMPROMISED) from exc
        if pointer['request_id'] in seen or len(result) >= em.MAX_CHAIN:
            raise RoomError(COMPROMISED)
        seen.add(pointer['request_id'])
        result.append(pointer)
        intent = em._read(directory, pointer['intent'])
        if not isinstance(intent, dict) or ao.digest(intent) != pointer['intent_sha256']:
            raise RoomError(COMPROMISED)
        pointer = intent.get('previous')
    return result


def _claim(service, directory, state, request_id, inputs):
    """Publish the one exact intent a crash may have written before its journal pointer."""
    claimed = {pointer['request_id'] + '.json' for pointer in _pointers(directory, state)}
    unclaimed = em._present(directory, 'requests') - claimed
    if not unclaimed:
        return state
    relative = BASE + '/requests/' + request_id + '.json'
    if unclaimed != {request_id + '.json'}:
        raise _NoWriteRefusal('An engineering model transition intent exists without its published request; reconcile the '
                              'identical request or diagnose the private journal before any other room change')
    intent = em._read(directory, relative)
    if not isinstance(intent, dict) or intent.get('request_id') != request_id:
        raise _NoWriteRefusal('An unclaimed engineering model transition intent belongs to another request; preserve it for '
                              'diagnosis')
    if intent.get('inputs') != inputs:
        raise _NoWriteRefusal('An unclaimed engineering model transition intent belongs to a different payload; only the '
                              'identical request can reconcile it')
    if intent.get('previous') != state.get(em.POINTER_KEY):
        raise _NoWriteRefusal('The unclaimed engineering model transition intent does not name this room current journal '
                              'predecessor; preserve and diagnose it')
    if _state_digest(state) != intent.get('before_state_sha256'):
        raise _NoWriteRefusal('Room state changed after the unclaimed engineering model transition intent was written; '
                              'preserve and diagnose it')
    em._intent(state, request_id, intent)
    em.store_once(directory, relative, intent)
    pointer = {'request_id': request_id, 'intent': relative, 'intent_sha256': ao.digest(intent),
               'state': 'pending', 'record': None, 'record_sha256': None}
    proposed = {**state, em.POINTER_KEY: pointer}
    em.journal(directory, proposed, allow_pending=True)
    service.save(directory, proposed)
    state.clear()
    state.update(proposed)
    return state


def _claim_pending_resolution(service, directory, state, pending):
    """Durably claim the single exact reader-validated historical resolution before any further audit."""
    path, sha, value = pending['path'], pending['sha256'], pending['value']
    stored = em.store_once(directory, path, value)
    if stored != sha or ao.digest(value) != sha:
        raise RoomError('The pending engineering model resolution changed before it could be claimed; preserve and '
                        'diagnose it')
    pointer = {'path': path, 'sha256': stored}
    proposed = {**state, em.RESOLUTIONS_KEY: list(state.get(em.RESOLUTIONS_KEY) or []) + [pointer]}
    em.journal(directory, proposed, allow_pending=True)
    service.save(directory, proposed)
    state.clear()
    state.update(proposed)
    return state


def _require_native_unchanged(stored, fresh, allow_target_settings, allow_abandonment):
    before, after = stored['native'], fresh['native']
    if set(before) != set(after):
        raise RoomError('The frozen engineering model transition evidence changed (native shape); a fresh audit is '
                        'required')
    for name in sorted(before):
        if name == 'settings':
            if allow_target_settings:
                expected = {**copy.deepcopy(before['settings']),
                            'model': stored['target']['configured_model'], 'reasoningEffort': em.EFFORT}
                if after['settings'] != expected:
                    raise RoomError('The frozen engineering model transition evidence changed (native.settings '
                                    'beyond the intended model/effort); a fresh audit is required')
            elif after['settings'] != before['settings']:
                raise RoomError('The frozen engineering model transition evidence changed (native.settings); a fresh '
                                'audit is required')
        elif name == 'controller' and allow_abandonment:
            if after['controller'] not in ('ready', 'stopped') or before['controller'] not in ('ready', 'stopped'):
                raise RoomError('Abandonment requires a ready or stopped controller')
        elif before[name] != after.get(name):
            raise RoomError('The frozen engineering model transition evidence changed (native.' + name
                            + '); a fresh audit is required')


def _require_owner_abandonment(stored, fresh):
    if set(stored) != set(fresh):
        raise RoomError('The retained native owner evidence changed shape; a fresh audit is required')
    if _stable_owner(stored) != _stable_owner(fresh):
        raise RoomError('Abandonment requires the unchanged retained native owner identity; only controller '
                        'liveness and generation may differ')
    if stored.get('activity_state') != 'idle' or fresh.get('activity_state') not in ('idle', 'exited'):
        raise RoomError('Abandonment requires an idle or exited retained native owner')
    for value in (stored.get('controller_generation'), fresh.get('controller_generation')):
        if not isinstance(value, str) or not value.strip():
            raise RoomError('Abandonment requires a positive retained controller generation')


def _require_unchanged(stored, fresh, *, allow_target_settings=False, allow_abandonment=False):
    """The full frozen evidence must remain identical, apart from documented target/abandonment liveness."""
    if set(stored) != set(fresh):
        raise RoomError('The frozen engineering model transition evidence changed shape; a fresh audit is required')
    for key in sorted(stored):
        if key == 'native':
            _require_native_unchanged(stored, fresh, allow_target_settings, allow_abandonment)
        elif key == 'native_owner' and allow_abandonment:
            _require_owner_abandonment(stored['native_owner'], fresh['native_owner'])
        elif key == 'qualification':
            _require_qualification_unchanged(stored[key], fresh[key])
        elif stored[key] != fresh[key]:
            raise RoomError('The frozen engineering model transition evidence changed (' + key
                            + '); a fresh audit is required')


def audit(service, room_id, target_model, native_owner_database, native_transcript_path):
    """Read-only eligibility and complete bound evidence for one engineering model transition."""
    ao.identifier(room_id)
    with service.locked(room_id) as (directory, state):
        try:
            evidence, target = _inspect(service, directory, state, target_model, native_owner_database,
                                        native_transcript_path, expect_target=False, allow_pending=False)
        except (RoomError, OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
            pending = None
            try:
                pending_chain = em.journal(directory, state, allow_pending=False)
            except (RoomError, OSError, ValueError, KeyError, TypeError):
                pending = None
            else:
                item = pending_chain.get('pending_resolution')
                if item is not None:
                    pending = {'path': item['path'], 'sha256': item['sha256'],
                               'request_id': item['value']['request_id']}
            result = {'eligible': False, 'room_id': state.get('room_id'), 'target_model': target_model,
                      'reason': str(exc), 'audit_sha256': None, 'evidence': None,
                      'native_owner_database': native_owner_database,
                      'native_transcript_path': native_transcript_path, 'model_dispatch': False,
                      'meaning': AUDIT_MEANING}
            if pending is not None:
                result['pending_resolution'] = pending
            return result
        digest = ao.digest(evidence)
        expectation = _frozen_expectation(target)
        return {'eligible': True, 'room_id': state['room_id'], 'target_model': target['configured_model'],
                'audit_sha256': digest, 'evidence': evidence,
                'source': copy.deepcopy(evidence['source']),
                'target': {key: copy.deepcopy(target[key]) for key in
                           ('selector', 'configured_model', 'reasoning_effort', 'harness', 'qualification',
                            'policy_sha256')},
                'reset': evidence['reset'], 'expected_model': expectation['expected_model'],
                'qualification_sha256': expectation['qualification_sha256'],
                'expected_model_basis': expectation['expected_model_basis'],
                'observed_served_model': None, 'served_model_basis': OBSERVED_SERVED,
                'effective_effort': {'configured': em.EFFORT, 'effective': 'not evidenced'},
                'spec_record_sha256': evidence['spec_record_sha256'],
                'candidate_sha256': evidence['candidate']['sha256'],
                'native_history_sha256': evidence['native']['history_sha256'],
                'native_transcript_sha256': evidence['native']['transcript_sha256'],
                'native_owner_database': native_owner_database,
                'native_transcript_path': native_transcript_path, 'controller': evidence['native']['controller'],
                'settings': copy.deepcopy(evidence['native']['settings']),
                'patch': None if evidence['reset'] else {'method': 'PATCH',
                                                         'path': _settings_path(evidence['engineer']),
                                                         'payload': _payload(target, evidence['native']['settings'])},
                'boundary_order': len(evidence['requests']),
                'native_owner': copy.deepcopy(evidence['native_owner']),
                'history': copy.deepcopy(evidence['history']), 'children': copy.deepcopy(evidence['children']),
                'ledger': copy.deepcopy(evidence['ledger']), 'executable': copy.deepcopy(evidence['executable']),
                'candidate': copy.deepcopy(evidence['candidate']),
                'preparation': copy.deepcopy(evidence['preparation']),
                'routing': copy.deepcopy(evidence['routing']), 'model_dispatch': False, 'meaning': AUDIT_MEANING}


def _pending_result(state, request_id, intent, marker, patch_result, reason, sent):
    return {'transitioned': False, 'abandoned': False, 'pending': True, 'idempotent': False,
            'room_id': state.get('room_id'), 'request_id': request_id,
            'intent': BASE + '/requests/' + request_id + '.json', 'intent_sha256': ao.digest(intent),
            'attempt': None if marker is None else BASE + '/attempts/' + request_id + '.json',
            'attempt_sha256': None if marker is None else ao.digest(marker),
            'record': None, 'record_sha256': None,
            'patch_result': copy.deepcopy(patch_result), 'observed': None, 'reason': reason,
            'settings_patch': 1 if sent else 0, 'model_dispatch': False, 'meaning': PENDING_MEANING}


def _committed_result(state, request_id, intent, marker, record, observation):
    expectation = _frozen_expectation(record['target'])
    return {'transitioned': True, 'abandoned': False, 'pending': False, 'idempotent': False,
            'room_id': state['room_id'], 'request_id': request_id,
            'intent': BASE + '/requests/' + request_id + '.json', 'intent_sha256': ao.digest(intent),
            'attempt': None if marker is None else BASE + '/attempts/' + request_id + '.json',
            'attempt_sha256': None if marker is None else ao.digest(marker),
            'record': BASE + '/records/' + request_id + '.json', 'record_sha256': ao.digest(record),
            'outcome': record['outcome'], 'patch_result': copy.deepcopy(record['patch_result']),
            'source': {'configured_model': record['source']['configured_model'],
                       'reasoning_effort': record['source']['reasoning_effort']},
            'target': {'configured_model': record['target']['configured_model'],
                       'reasoning_effort': record['target']['reasoning_effort']},
            'observed': {'settings': copy.deepcopy(observation['settings']),
                         'controller': observation['controller']},
            'configured_model': record['target']['configured_model'],
            'expected_model': expectation['expected_model'],
            'qualification_sha256': expectation['qualification_sha256'],
            'expected_model_basis': expectation['expected_model_basis'],
            'observed_served_model': None, 'served_model_basis': OBSERVED_SERVED,
            'effective_effort': {'configured': em.EFFORT, 'effective': 'not evidenced'},
            'adoption': 'AO settings read-back recorded; not native execution',
            'boundary_order': record['boundary_order'], 'reset_epoch': record['attempt_sha256'] is None,
            'settings_patch': 0, 'model_dispatch': False,
            'meaning': ('The configured engineering value, its pinned qualification and one new epoch are committed. '
                        'AO settings read-back is not native adoption: only the next owned response establishes use.')}


def _settle(service, directory, state, request_id, intent, patch_result, owner_database, transcript_path,
            marker=None, stored=None):
    """Observe once, then commit the durable record or report the attempt as still pending.

    ``stored`` is only ever the exact durable committed record a crash wrote before its pointer. A
    validated durable record of any other outcome never enters this committed settlement path: its
    own terminal outcome is recovered as exactly that outcome, so it is refused here rather than
    rewritten, relabelled as a commit or silently replaced.
    """
    if stored is not None and (not isinstance(stored, dict)
                               or stored.get('outcome') != em.OUTCOMES['committed']):
        raise RoomError('A validated durable engineering model transition record that is not committed is '
                        'recovered as exactly its own terminal outcome, never through the committed settlement '
                        'path')
    reset = intent['patch'] is None
    before = _before(intent['evidence'])
    expectation = None if reset else {'model': intent['target']['configured_model'],
                                      'reasoning_effort': em.EFFORT}
    expected_settings = (copy.deepcopy(before['settings']) if reset else
                         {**copy.deepcopy(before['settings']), 'model': intent['target']['configured_model'],
                          'reasoningEffort': em.EFFORT})
    try:
        after, _ = _observe(service, directory, state, intent['evidence']['engineer'], expectation, owner_database,
                            transcript_path, intent['evidence']['preparation']['worktree'])
    except RoomError as exc:
        return None, ('The attempt stays pending: the fresh native observation did not equal the recorded target ('
                      + str(exc)[:300] + ')')
    if stored is not None:
        if after != stored['observed_after']:
            return None, ('The attempt stays pending: the durable record observation no longer holds for the retained '
                          'owner, history, transcript or settings')
    elif (after['settings'] != expected_settings or after['controller'] != 'ready'
          or after['history_sha256'] != before['history_sha256']
          or after['transcript_sha256'] != before['transcript_sha256']
          or after['native_owner'] != before['native_owner']):
        return None, ('The attempt stays pending: a commit requires the target settings with an unchanged owner, '
                      'history, transcript and a ready controller')
    try:
        frozen = _inspect(service, directory, state, intent['target']['configured_model'], owner_database,
                          transcript_path, expect_target=not reset, allow_pending=True,
                          target_override=intent['target'], pending_request_id=request_id)[0]
        _require_unchanged(intent['evidence'], frozen, allow_target_settings=not reset)
    except RoomError as exc:
        return None, 'The attempt stays pending: ' + str(exc)[:400]
    if stored is None:
        record = {'version': 1, 'room_id': state['room_id'], 'request_id': request_id,
                  'intent_sha256': ao.digest(intent),
                  'attempt_sha256': None if marker is None else ao.digest(marker),
                  'outcome': em.OUTCOMES['committed'], 'patch_result': copy.deepcopy(patch_result),
                  'observed_before': before, 'observed_after': after,
                  'source': copy.deepcopy(intent['source']), 'target': copy.deepcopy(intent['target']),
                  'boundary_order': len(intent['evidence']['requests']), 'recorded_at': time.time()}
    else:
        record = stored
    em.store_once(directory, BASE + '/records/' + request_id + '.json', record)
    pointer = {'request_id': request_id, 'intent': BASE + '/requests/' + request_id + '.json',
               'intent_sha256': ao.digest(intent), 'state': 'committed',
               'record': BASE + '/records/' + request_id + '.json', 'record_sha256': ao.digest(record)}
    published = {**state, em.POINTER_KEY: pointer}
    em.journal(directory, published)
    service.save(directory, published)
    return _committed_result(published, request_id, intent, marker, record, after), None


def _advance(service, directory, state, request_id, intent, marker, owner_database, transcript_path):
    """At most one PATCH for this request, then observe and commit or stay pending."""
    reset = intent['patch'] is None
    patch_result = dict(NO_PATCH)
    sent = False
    if not reset and marker is None:
        fresh = _inspect(service, directory, state, intent['target']['configured_model'], owner_database,
                         transcript_path, expect_target=False, allow_pending=True,
                         target_override=intent['target'], pending_request_id=request_id)[0]
        _require_unchanged(intent['evidence'], fresh)
        marker = {'version': 1, 'room_id': state['room_id'], 'request_id': request_id,
                  'intent_sha256': ao.digest(intent), 'patch': copy.deepcopy(intent['patch']),
                  'started_at': time.time()}
        em.store_once(directory, BASE + '/attempts/' + request_id + '.json', marker)
        try:
            response = service.client(state).request('PATCH', intent['patch']['path'], intent['patch']['payload'])
        except RoomError as exc:
            patch_result = {'acknowledged': False, 'error': str(exc)[:500], 'response_sha256': None}
        else:
            patch_result = {'acknowledged': True, 'error': None, 'response_sha256': ao.digest(response)}
        sent = True
    elif not reset:
        patch_result = {'acknowledged': False,
                        'error': 'Exactly one PATCH was already attempted for this request; this record states a '
                                 'fresh read-only observation, not the original acknowledgement',
                        'response_sha256': None}
    result, reason = _settle(service, directory, state, request_id, intent, patch_result, owner_database,
                             transcript_path, marker=marker)
    if result is None:
        return _pending_result(state, request_id, intent, marker, patch_result, reason, sent)
    return result


def _apply(service, directory, state, request_id, inputs, target_model, owner_database, transcript_path):
    """A new request: prove the audit, write the create-once intent, then run one attempt."""
    try:
        evidence, target = _inspect(service, directory, state, target_model, owner_database, transcript_path,
                                    expect_target=False, allow_pending=False)
    except (RoomError, OSError, ValueError, KeyError, TypeError) as exc:
        raise _NoWriteRefusal(str(exc)) from exc
    if inputs['source_model'] != evidence['source']['configured_model']:
        raise _NoWriteRefusal('The named source model is not the engineering epoch in force; audit the room again')
    if inputs['target_model'] != evidence['target']['configured_model']:
        raise _NoWriteRefusal('The named target model is not the audited qualified selector; audit the room again')
    if ao.digest(evidence) != inputs['audit_sha256']:
        raise _NoWriteRefusal('The engineering model transition audit is stale; inspect the changed room and audit again')
    if inputs['spec_record_sha256'] != evidence['spec_record_sha256']:
        raise _NoWriteRefusal('The named specification record digest is not the audited value; audit the room again')
    if inputs['candidate_sha256'] != evidence['candidate']['sha256']:
        raise _NoWriteRefusal('The named candidate digest is not the audited value; audit the room again')
    if inputs['native_history_sha256'] != evidence['native']['history_sha256']:
        raise _NoWriteRefusal('The named native history digest is not the audited value; audit the room again')
    _retain_qualified_target(directory, target)
    reset = evidence['reset']
    patch = None if reset else {'method': 'PATCH', 'path': _settings_path(evidence['engineer']),
                               'payload': _payload(target, evidence['native']['settings'])}
    intent = {'version': 1, 'room_id': state['room_id'], 'request_id': request_id, 'inputs': inputs,
              'inputs_sha256': ao.digest(inputs), 'audit_sha256': inputs['audit_sha256'], 'evidence': evidence,
              'before_state_sha256': _state_digest(state), 'previous': state.get(em.POINTER_KEY),
              'source': copy.deepcopy(evidence['source']), 'target': copy.deepcopy(target), 'patch': patch,
              'recorded_at': time.time(),
              'provider_transition_receipt_sha256': (state.get('provider_transition') or {}).get('receipt_sha256')}
    em._intent(state, request_id, intent)
    em.store_once(directory, BASE + '/requests/' + request_id + '.json', intent)
    pointer = {'request_id': request_id, 'intent': BASE + '/requests/' + request_id + '.json',
               'intent_sha256': ao.digest(intent), 'state': 'pending', 'record': None, 'record_sha256': None}
    published = {**state, em.POINTER_KEY: pointer}
    em.journal(directory, published, allow_pending=True)
    service.save(directory, published)
    return _advance(service, directory, published, request_id, intent, None, owner_database, transcript_path)


def _reconcile(service, directory, state, attempt, owner_database, transcript_path):
    """The identical request against a pending attempt: never a second PATCH.

    A crash may have written this request's own durable terminal record before its pointer was
    saved. A committed record is settled exactly as before from its frozen observation. A validated
    abandonment record never enters that committed settlement path: the identical request crosses
    the same full fresh abandonment guard as the explicit reconciliation and republishes exactly
    those recorded bytes under an abandoned pointer, with the record's own recorded authorization
    and diagnosis as the existing authority. Neither path sends a PATCH.
    """
    intent, marker, record = attempt['intent_value'], attempt['attempt_value'], attempt['record_value']
    if record is not None:
        if isinstance(record, dict) and record.get('outcome') == em.OUTCOMES['abandoned']:
            abandonment = record['abandonment']
            return _abandon_locked(service, directory, state, attempt['request_id'], owner_database,
                                   transcript_path, abandonment['authorization'], abandonment['diagnosis'])
        result, reason = _settle(service, directory, state, attempt['request_id'], intent,
                                 record['patch_result'], owner_database, transcript_path, marker=marker,
                                 stored=record)
        if result is None:
            return _pending_result(state, attempt['request_id'], intent, marker, record['patch_result'], reason,
                                   False)
        return result
    return _advance(service, directory, state, attempt['request_id'], intent, marker, owner_database, transcript_path)


def _reaudit_result(state, request_id, pending):
    """A durable historical resolution was adopted; the previous audit no longer describes the room."""
    return {'transitioned': False, 'abandoned': False, 'pending': False, 'reaudit_required': True,
            'room_id': state.get('room_id'), 'request_id': request_id,
            'pending_resolution': {'path': pending['path'], 'sha256': pending['sha256']},
            'settings_patch': 0, 'model_dispatch': False,
            'reason': 'A validated historical engineering model resolution was adopted before this transition; '
                      'audit the room again and submit the fresh audit digest. The previous audit no longer applies.',
            'meaning': 'The historical resolution is now claimed durably. This request made no PATCH and wrote no '
                       'transition intent; a fresh read-only audit is required.'}


def _terminal_result(state, attempt):
    record = attempt['record_value']
    committed = record['outcome'] == em.OUTCOMES['committed']
    expectation = _frozen_expectation(record['target'])
    observed = record['observed_after']
    return {'transitioned': committed, 'abandoned': not committed, 'pending': False, 'idempotent': True,
            'room_id': state['room_id'], 'request_id': attempt['request_id'], 'intent': attempt['intent'],
            'intent_sha256': attempt['intent_sha256'],
            'attempt': None if attempt['attempt_sha256'] is None
                       else BASE + '/attempts/' + attempt['request_id'] + '.json',
            'attempt_sha256': attempt['attempt_sha256'],
            'record': BASE + '/records/' + attempt['request_id'] + '.json', 'record_sha256': ao.digest(record),
            'outcome': record['outcome'], 'patch_result': copy.deepcopy(record['patch_result']),
            'source': {'configured_model': record['source']['configured_model'],
                       'reasoning_effort': record['source']['reasoning_effort']},
            'target': {'configured_model': record['target']['configured_model'],
                       'reasoning_effort': record['target']['reasoning_effort']},
            'observed': {'settings': copy.deepcopy(observed['settings']),
                         'controller': observed['controller']},
            # The top-level configured model describes the frozen terminal record's own actual
            # observed configuration, never the intended target of an abandoned attempt: this
            # idempotent historical read never observes AO again.
            'configured_model': observed['settings'].get('model'),
            'configured_model_basis': (("The frozen terminal record's actual observed configuration; it is the "
                                        "committed configured value") if committed else
                                       ("The frozen terminal record's actual observed configuration; the intended "
                                        "target was abandoned, not adopted")),
            'intended_target': {'configured_model': record['target']['configured_model'],
                                'reasoning_effort': record['target']['reasoning_effort'],
                                'expected_model': expectation['expected_model'],
                                'qualification_sha256': expectation['qualification_sha256'],
                                'committed': committed,
                                'basis': (("Frozen request target and its pinned pre-inference expectation; the "
                                           "committed configured value is AO settings read-back, not native "
                                           "execution") if committed else
                                          ("Frozen request target and its pinned pre-inference expectation; this "
                                           "terminal record abandoned it without adoption"))},
            'expected_model': expectation['expected_model'],
            'qualification_sha256': expectation['qualification_sha256'],
            'expected_model_basis': expectation['expected_model_basis'],
            'observed_served_model': None, 'served_model_basis': OBSERVED_SERVED,
            'effective_effort': {'configured': em.EFFORT, 'effective': 'not evidenced'},
            'adoption': ('AO settings read-back recorded; not native execution' if committed else
                         'none: this record abandons the intended target and does not adopt it'),
            'boundary_order': record['boundary_order'], 'settings_patch': 0, 'model_dispatch': False,
            'meaning': ('This request_id already has one durable terminal record; nothing was written, patched or '
                        'dispatched again.')}


def _transition_locked(service, directory, state, request_id, source_model, target_model, audit_sha256,
                       spec_record_sha256, candidate_sha256, native_history_sha256, native_owner_database,
                       native_transcript_path, authorization, reason, inputs):
    state = _claim(service, directory, state, request_id, inputs)
    chain = em.journal(directory, state, allow_pending=True)
    if chain.get('pending_resolution') is not None:
        if chain.get('pending') is not None:
            raise _NoWriteRefusal('A validated historical engineering model resolution and an uncommitted transition '
                                  'both exist; reconcile the resolution before this transition')
        state = _claim_pending_resolution(service, directory, state, chain['pending_resolution'])
        return _reaudit_result(state, request_id, chain['pending_resolution'])
    service.settled(state, pending_model_transition=True)
    chain = em.journal(directory, state, allow_pending=True)
    attempt = next((item for item in chain['attempts'] if item['request_id'] == request_id), None)
    if attempt is not None:
        if attempt['intent_value']['inputs'] != inputs:
            raise _NoWriteRefusal('This request_id already belongs to a different engineering model transition '
                                  'payload; only the identical request can be read or reconciled')
        if attempt['state'] != 'pending':
            return _terminal_result(state, attempt)
        return _reconcile(service, directory, state, attempt, native_owner_database, native_transcript_path)
    if chain['pending'] is not None:
        raise _NoWriteRefusal('An uncommitted engineering model transition exists (' + chain['pending']['request_id']
                              + '); reconcile only that identical request before any other room change')
    return _apply(service, directory, state, request_id, inputs, target_model, native_owner_database,
                  native_transcript_path)


def transition(service, room_id, request_id, source_model, target_model, audit_sha256, spec_record_sha256,
               candidate_sha256, native_history_sha256, native_owner_database, native_transcript_path,
               authorization, reason):
    """Apply or reconcile exactly one audited engineering model transition."""
    ao.identifier(room_id)
    ao.identifier(request_id)
    ao.nonempty(authorization, 'authorization: the actual user decision for this exact change', 6000)
    ao.nonempty(reason, 'reason', 6000)
    for name, value in (('native_owner_database', native_owner_database),
                        ('native_transcript_path', native_transcript_path)):
        ao.nonempty(value, name, 4096)
    for name, value in (('source_model', source_model), ('audit_sha256', audit_sha256),
                        ('spec_record_sha256', spec_record_sha256), ('candidate_sha256', candidate_sha256),
                        ('native_history_sha256', native_history_sha256)):
        if not isinstance(value, str) or not value:
            raise RoomError(name + ' must be the exact value returned by the audit')
    for name, value in (('audit_sha256', audit_sha256), ('spec_record_sha256', spec_record_sha256),
                        ('candidate_sha256', candidate_sha256), ('native_history_sha256', native_history_sha256)):
        if not DIGEST.fullmatch(value):
            raise RoomError(name + ' must be the exact SHA256 digest returned by the audit')
    inputs = {'request_id': request_id, 'source_model': source_model, 'target_model': target_model,
              'audit_sha256': audit_sha256, 'spec_record_sha256': spec_record_sha256,
              'candidate_sha256': candidate_sha256, 'native_history_sha256': native_history_sha256,
              'native_owner_database': native_owner_database, 'native_transcript_path': native_transcript_path,
              'authorization': authorization, 'reason': reason}
    deferred = None
    with service.locked(room_id) as (directory, state):
        try:
            return _transition_locked(service, directory, state, request_id, source_model, target_model,
                                      audit_sha256, spec_record_sha256, candidate_sha256,
                                      native_history_sha256, native_owner_database, native_transcript_path,
                                      authorization, reason, inputs)
        except _NoWriteRefusal as exc:
            deferred = exc
    raise deferred


def _abandon_locked(service, directory, state, request_id, native_owner_database, native_transcript_path,
                    authorization, diagnosis):
    service.settled(state, pending_model_transition=True)
    chain = em.journal(directory, state, allow_pending=True)
    attempt = next((item for item in chain['attempts'] if item['request_id'] == request_id), None)
    if attempt is None:
        raise _NoWriteRefusal('Abandonment requires the exact pending engineering model transition request')
    intent = attempt['intent_value']
    if (intent['inputs']['native_owner_database'] != native_owner_database
            or intent['inputs']['native_transcript_path'] != native_transcript_path):
        raise _NoWriteRefusal('Abandonment must use the exact native owner database and transcript of this request')
    abandonment = {'authorization': authorization, 'diagnosis': diagnosis,
                   'abandon_inputs_sha256': ao.digest({'request_id': request_id,
                                                       'intent_sha256': attempt['intent_sha256'],
                                                       'attempt_sha256': attempt['attempt_sha256'],
                                                       'native_owner_database': native_owner_database,
                                                       'native_transcript_path': native_transcript_path,
                                                       'authorization': authorization, 'diagnosis': diagnosis})}
    record = attempt['record_value']
    if attempt['state'] != 'pending' or record is not None:
        if (record is None or record.get('outcome') != em.OUTCOMES['abandoned']
                or record.get('abandonment') != abandonment):
            raise _NoWriteRefusal('This engineering model transition request already has a different terminal record')
        if attempt['state'] != 'pending':
            return _terminal_result(state, attempt)
    try:
        frozen, _ = _inspect(service, directory, state, intent['target']['configured_model'], native_owner_database,
                             native_transcript_path, expect_target=False, allow_pending=True,
                             target_override=intent['target'], allow_stopped_controller=True,
                             allow_exited_owner=True, pending_request_id=request_id)
        _require_unchanged(intent['evidence'], frozen, allow_abandonment=True)
    except RoomError as exc:
        raise RoomError('Abandonment requires the unchanged frozen evidence: ' + str(exc)[:400]) from exc
    before = _before(intent['evidence'])
    after = {'settings': copy.deepcopy(frozen['native']['settings']),
             'controller': frozen['native']['controller'],
             'history_sha256': frozen['native']['history_sha256'],
             'transcript_sha256': frozen['native']['transcript_sha256'],
             'native_owner': copy.deepcopy(frozen['native_owner'])}
    if record is None:
        record = {'version': 1, 'room_id': state['room_id'], 'request_id': request_id,
                  'intent_sha256': attempt['intent_sha256'], 'attempt_sha256': attempt['attempt_sha256'],
                  'outcome': em.OUTCOMES['abandoned'], 'patch_result': dict(NO_PATCH),
                  'observed_before': before, 'observed_after': after,
                  'source': copy.deepcopy(intent['source']), 'target': copy.deepcopy(intent['target']),
                  'boundary_order': len(intent['evidence']['requests']), 'recorded_at': time.time(),
                  'abandonment': abandonment}
    # A crash may have written this exact record before its journal pointer. The bytes and their recorded
    # time are immutable; republish exactly them (crossing every ancestor barrier again) and never replace
    # a conflicting terminal record or a changed abandonment payload.
    em.store_once(directory, BASE + '/records/' + request_id + '.json', record)
    pointer = {'request_id': request_id, 'intent': BASE + '/requests/' + request_id + '.json',
               'intent_sha256': attempt['intent_sha256'], 'state': 'abandoned',
               'record': BASE + '/records/' + request_id + '.json', 'record_sha256': ao.digest(record)}
    published = {**state, em.POINTER_KEY: pointer}
    em.journal(directory, published)
    service.save(directory, published)
    return {'transitioned': False, 'abandoned': True, 'pending': False, 'idempotent': False,
            'room_id': state['room_id'], 'request_id': request_id, 'intent': pointer['intent'],
            'intent_sha256': attempt['intent_sha256'],
            'attempt': None if attempt['attempt_sha256'] is None
                       else BASE + '/attempts/' + request_id + '.json',
            'attempt_sha256': attempt['attempt_sha256'],
            'record': BASE + '/records/' + request_id + '.json', 'record_sha256': ao.digest(record),
            'outcome': record['outcome'], 'patch_result': dict(NO_PATCH),
            'patch_attempted': attempt['attempt_sha256'] is not None,
            'observed': {'settings': copy.deepcopy(after['settings']), 'controller': after['controller']},
            'abandon_inputs_sha256': abandonment['abandon_inputs_sha256'], 'settings_patch': 0,
            'model_dispatch': False, 'meaning': ABANDON_MEANING}


def abandon(service, room_id, request_id, native_owner_database, native_transcript_path, authorization, diagnosis):
    """Close only the exact pending request after a fresh full unchanged-source observation."""
    ao.identifier(room_id)
    ao.identifier(request_id)
    ao.nonempty(authorization, 'authorization: the actual user decision to abandon this attempt', 6000)
    ao.nonempty(diagnosis, 'diagnosis', 6000)
    ao.nonempty(native_owner_database, 'native_owner_database', 4096)
    ao.nonempty(native_transcript_path, 'native_transcript_path', 4096)
    deferred = None
    with service.locked(room_id) as (directory, state):
        try:
            return _abandon_locked(service, directory, state, request_id, native_owner_database,
                                   native_transcript_path, authorization, diagnosis)
        except _NoWriteRefusal as exc:
            deferred = exc
    raise deferred
