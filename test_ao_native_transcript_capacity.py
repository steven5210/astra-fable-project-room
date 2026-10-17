"""Synthetic production-path proofs for the native transcript capacity REPAIR (post-repair contract).

Today, three production readers refuse any native transcript over 64,000,000 bytes before they read
any content: ao_native_outcome.inspect, ao_native_outcome.preflight_source/validate_registered_source,
and ao_engineering_transition._read_transcript (consumed by child_evidence). Claude Code transcripts
regularly grow past that size on long sessions, so every one of those readers' own scenario-specific
logic (quota detection, autocompact-thrashing proof, model qualification, Agent/Task launch
correlation, malformed/oversized-row handling, ...) is silently masked behind one blanket
ownership/size refusal the moment the file crosses the line. ao_progress._window is the deliberate
counter-example: it never refuses on size, reading only a bounded tail window instead, and must keep
doing exactly that, unchanged (P7).

This module proves the REPAIR, not the defect: every giant-fixture (>64,000,000 byte) assertion below
is written against the post-repair contract, so it is RED on this unchanged baseline (today's low cap
still masks the scenario, or a now-stale constant/message does not yet match) and is expected to turn
GREEN once the shared-reader repair lands. The post-repair contract exercised here:

  1. ao_native_outcome.MAX_TRANSCRIPT_BYTES and ao_engineering_transition.MAX_TRANSCRIPT_BYTES both
     become 256_000_000 (today: ano has no such constant; et's is 64_000_000).
  2. New constants ano.MAX_TRANSCRIPT_RECORD_BYTES == 16_777_216 and ano.MAX_TRANSCRIPT_RECORDS ==
     2_000_000 exist and bound one record's bytes and one file's total record count respectively.
  3. New string refusal constants ano.TRANSCRIPT_RECORD_BYTES_REFUSAL, ano.TRANSCRIPT_RECORD_COUNT_REFUSAL
     and ano.TRANSCRIPT_MALFORMED_REFUSAL exist and are raised (as a RoomError's args[0]) by both the
     inspect lane and the _read_transcript lane for their respective conditions.
  4. Every P1-P3 scenario (quota, autocompact-thrashing, model qualification, Agent/Task launch
     correlation, ...), run on a giant (>64,000,000 byte) history, succeeds and matches an
     identical-content small control on every content-derived field, instead of being masked.
  5. A mutation between two observations of a giant history still changes the observed digest, exactly
     like the existing small-scale mutation proofs.
  6. Malformed JSON, a valid-JSON-non-object row, and the new per-record byte boundary, each embedded
     inside an otherwise-giant history, are refused (or admitted, exactly at the boundary) by both
     lanes via the new constants above -- masked by size on this baseline so they fail there too,
     for the same whole-file-cap reason as P1-P3, not yet by their own row-level checks.
  7. ao_progress._window keeps reading a bounded tail unconditionally (regression guard, unchanged).
  8. A sparse 256,000,001 byte file (one byte over the new cap) is refused by every vulnerable reader's
     size check before any content is read -- true on this baseline too (the old, lower cap already
     refuses it), so this boundary anchor passes on both baseline and after repair.
  9. The new per-file record-count cap: exactly 2,000,000 records is admitted; 2,000,001 is refused via
     TRANSCRIPT_RECORD_COUNT_REFUSAL in both lanes.
  10. (Operator-run only, skipped by default) an exact 256,000,000 byte file is admitted and a
      256,000,001 byte file is refused, with wall time and peak RSS recorded.

Constants that do not exist on this baseline are read via getattr(module, 'NAME', None), so this
module always imports cleanly before and after the repair and only ever fails at an assertion, never
at import. Where a giant proof has a small control fixture of identical content, the giant result is
asserted equal to the control result on every field that matters (errors, stop_reasons,
next_human_uuid, compaction_failure, model_contradiction, launch/result counts), excluding fields that
legitimately differ because the two fixtures use independent temporary workspaces (workspace_sha256
inside compaction_failure). A handful of tests are unrelated to the byte cap (identity/mode/mutation
checks on small fixtures); these are explicitly labeled "# control: passes on baseline and after
repair" and are not inverted. All synthetic identifiers, paths and timestamps below are fabricated for
this module; none are real machine paths or credentials.

P1(d)/P2 (QualifiedFamilyCapacityTests) run their test_ao_normal.Fixture-based proof in a child
process rather than inline. Empirically, building any test_ao_engineering_transition.TransitionCase
earlier in this same process -- even fully cleaned up -- leaves state that makes a later Fixture's own
.bind() fail inside ao_routing.prepare with a bare `KeyError: 'contradictory_env'`, and the reverse
order corrupts later TransitionCase-based owner checks instead. That TransitionCase/Fixture
same-process interaction is characterized in detail in this module's delivery report and is not fixed
here (out of scope: it is a property of the baseline fixtures coexisting, not of this module's own
cleanup order -- see below). Running the one Fixture-based proof as a separate process sidesteps it
without editing any other file, while still calling the same real, unmodified production entry points,
and stays in place as defense in depth regardless of the fix described next.

This module's own classes that keep two TransitionCase instances alive at once
(NativeOutcomeCapacityTests, ChildEvidenceCapacityTests: cls.giant set up first, then cls.small) must
call doCleanups() in the strict reverse of that setup order (cls.small first, cls.giant last).
mock.patch.object's .stop() restores an attribute to whatever value it captured at .start() time --
the value in place immediately before that particular patch began. cls.small's four patches start
while cls.giant's are already active, so cls.small's captured "original" is cls.giant's stub, not the
real ao_routing.observe_rules/validate_local/rules_match or ao_engineering_model.executable_identity.
Cleaning up cls.giant before cls.small therefore leaves cls.giant's stub permanently installed, once
cls.small.doCleanups() runs last and "restores" each attribute back to it. That was this module's own
cleanup-order bug -- not a pre-existing harness gap -- and is fixed below by reversing the two
doCleanups() calls; tearDownModule() also asserts the four attributes are back to their recorded
original objects, so any future reintroduction of this mistake fails inside this module instead of
cascading into every module that sorts after it and calls ao_routing.prepare().
"""
import copy
import hashlib
import json
import os
import subprocess
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import ao_engineering_model as em
import ao_engineering_transition as et
import ao_native_outcome as ano
import ao_outcomes
import ao_progress
import ao_project_room as ao
import ao_routing
import ao_spec_verdict_recovery as recovery
from room import RoomError

from test_ao_engineering_transition import TransitionCase, FABLE, NATIVE, SESSION, TEXT
# ao_engineering_model (em) and ao_routing are imported above only so the tearDownModule guard below
# can record and recheck the identity of the four attributes TransitionCase.setUp patches; nothing in
# this module calls either module directly or builds a Fixture from them. test_ao_normal.Fixture
# itself is still imported only inside P1D_P2_CHILD_SCRIPT (a separate process); see that script and
# the module docstring's paragraph on why QualifiedFamilyCapacityTests still runs isolated.


# ---------------------------------------------------------------------------------------------
# Cleanup-order guard: fails inside this module, naming the leaked attribute, if any class below
# ever again stops two overlapping TransitionCase instances' patches out of strict reverse-of-setup
# order (see the module docstring and NativeOutcomeCapacityTests.tearDownClass). Recorded here, at
# import time, before any class below has started a single patch.
# ---------------------------------------------------------------------------------------------

_GUARDED_ATTRS = (
    (ao_routing, 'observe_rules'),
    (ao_routing, 'validate_local'),
    (ao_routing, 'rules_match'),
    (em, 'executable_identity'),
)
_ORIGINAL_ATTR_VALUES = tuple(getattr(module, name) for module, name in _GUARDED_ATTRS)


def tearDownModule():
    for (module, name), original in zip(_GUARDED_ATTRS, _ORIGINAL_ATTR_VALUES):
        current = getattr(module, name)
        if current is not original:
            raise AssertionError(
                "leaked patch: %s.%s was not restored to its original object by the end of this "
                "module (a doCleanups() call ran out of strict reverse-of-setup order)"
                % (module.__name__, name))


# ---------------------------------------------------------------------------------------------
# Shared synthetic-row helpers
# ---------------------------------------------------------------------------------------------

def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _line(row):
    return (json.dumps(row, sort_keys=True, ensure_ascii=False) + '\n').encode('utf-8')


BASE_T = 1_735_000_000.0
# Comfortably before every created_at used in this module (TransitionCase defaults to
# 1,700,000,000.0; this module's own scenarios start at BASE_T, further still), so every padding
# row safely precedes every scenario's own anchor regardless of which fixture reuses it.
PADDING_TIMESTAMP = _iso(1_600_000_000.0)
# One reusable ~50,000-byte block, sliced rather than rebuilt per row, so writing 65-72MB of
# realistic padding takes a fraction of a second (measured ~0.2s for 70,038,378 bytes).
PAD_CONTENT = ('Synthetic padding content for a realistic mixed native transcript row. ' * 900)[:50_000]
MODULE_DIR = Path(__file__).resolve().parent


def padding_pair(session_id, workspace, index):
    """One harmless Read tool_use/tool_result pair: same session/workspace, inert to every outcome lane.

    Always carries PADDING_TIMESTAMP. ao_native_outcome.compaction_failure's two full-file scanning
    loops unconditionally refuse (return None) on any same-session assistant-type row with a missing
    or malformed timestamp, regardless of that row's position relative to the scenario under proof;
    a real, valid, deep-past timestamp on every padding row avoids that landmine while keeping the
    padding a realistic mixed row of the same session and workspace.
    """
    tool_id = 'toolu_pad_%08d' % index
    use_uuid = 'pad-use-%08d' % index
    result_uuid = 'pad-result-%08d' % index
    use = {'type': 'assistant', 'uuid': use_uuid, 'sessionId': session_id, 'cwd': workspace,
           'isSidechain': False, 'timestamp': PADDING_TIMESTAMP,
           'message': {'role': 'assistant', 'content': [
               {'type': 'tool_use', 'id': tool_id, 'name': 'Read',
                'input': {'file_path': '/synthetic/padding/file_%08d.txt' % index}}]}}
    result = {'type': 'user', 'uuid': result_uuid, 'sessionId': session_id, 'cwd': workspace,
              'isSidechain': False, 'timestamp': PADDING_TIMESTAMP, 'sourceToolAssistantUUID': use_uuid,
              'message': {'role': 'user', 'content': [
                  {'type': 'tool_result', 'tool_use_id': tool_id, 'content': PAD_CONTENT}]}}
    return _line(use) + _line(result)


def write_padding_bytes(fh, session_id, workspace, approx_bytes, start_index):
    """Append realistic mixed padding rows until approx_bytes more have been written, batched for speed."""
    if approx_bytes <= 0:
        return start_index, 0
    index = start_index
    written = 0
    batch, batch_bytes = [], 0
    while written < approx_bytes:
        index += 1
        block = padding_pair(session_id, workspace, index)
        batch.append(block)
        batch_bytes += len(block)
        written += len(block)
        if batch_bytes >= 8_000_000:
            fh.write(b''.join(batch))
            batch, batch_bytes = [], 0
    if batch:
        fh.write(b''.join(batch))
    return index, written


def scenario_rows(key, request, session_id, workspace):
    """The small turn for one P1 native-outcome scenario (a/b/c/e); see class docstrings."""
    created_at = request['created_at']
    anchor = {'type': 'user', 'uuid': 'turn-%s-anchor' % key, 'sessionId': session_id, 'cwd': workspace,
              'isSidechain': False, 'timestamp': _iso(created_at + 0.001), 'origin': {'kind': 'human'},
              'message': {'role': 'user', 'content': request['text']}}
    if key == 'a':  # clean completed turn
        stop = {'type': 'assistant', 'uuid': 'turn-a-stop', 'sessionId': session_id, 'cwd': workspace,
                'isSidechain': False, 'timestamp': _iso(created_at + 0.5),
                'message': {'role': 'assistant', 'model': FABLE, 'id': 'turn-a-m0', 'stop_reason': 'end_turn',
                            'content': [{'type': 'text', 'text': 'Synthetic completed answer.'}]}}
        return [anchor, stop]
    if key == 'b':  # quota failure (429 / rate_limit)
        err = {'type': 'assistant', 'uuid': 'turn-b-error', 'sessionId': session_id, 'cwd': workspace,
               'isSidechain': False, 'timestamp': _iso(created_at + 0.5), 'isApiErrorMessage': True,
               'error': 'rate_limit', 'apiErrorStatus': 429,
               'message': {'role': 'assistant', 'model': '<synthetic>',
                           'content': [{'type': 'text', 'text': 'Synthetic rate_limit evidence.'}]}}
        return [anchor, err]
    if key == 'e':  # incomplete turn (stopped on tool_use, no final answer yet)
        stop = {'type': 'assistant', 'uuid': 'turn-e-stop', 'sessionId': session_id, 'cwd': workspace,
                'isSidechain': False, 'timestamp': _iso(created_at + 0.5),
                'message': {'role': 'assistant', 'model': FABLE, 'stop_reason': 'tool_use',
                            'content': [{'type': 'tool_use', 'id': 'turn-e-launch', 'name': 'Agent',
                                         'input': {'prompt': 'Investigate further.'}}]}}
        return [anchor, stop]
    if key == 'c':  # exact autocompact-thrashing synthetic error
        err = {'type': 'assistant', 'uuid': 'turn-c-error', 'sessionId': session_id, 'cwd': workspace,
               'isSidechain': False, 'timestamp': _iso(created_at + 0.5), 'isApiErrorMessage': True,
               'error': 'invalid_request',
               'message': {'role': 'assistant', 'model': '<synthetic>', 'stop_reason': 'stop_sequence',
                           'content': [{'type': 'text', 'text': ano.COMPACTION_THRASHING_TEXT}]}}
        return [anchor, err]
    raise ValueError(key)


def completed_result(agent_id, prompt):
    """The exact claude_agent_completed_v1 toolUseResult shape (see
    test_ao_evidence_audit_independent.completed), with an explicit prompt for the launch it closes."""
    return {'status': 'completed', 'agentId': agent_id, 'prompt': prompt, 'content': [],
            'totalToolUseCount': 0, 'totalDurationMs': 0, 'totalTokens': 0,
            'usage': {'input_tokens': 0, 'output_tokens': 0, 'cache_creation_input_tokens': None,
                      'cache_read_input_tokens': None, 'server_tool_use': None,
                      'service_tier': None, 'cache_creation': None}}


def foreground_completed_rows(workspace):
    use = {'type': 'assistant', 'uuid': 'fg-use', 'sessionId': NATIVE, 'cwd': workspace, 'isSidechain': False,
           'message': {'role': 'assistant', 'content': [
               {'type': 'tool_use', 'id': 'toolu_fg_launch', 'name': 'Agent',
                'input': {'prompt': 'bounded task'}}]}}
    result = {'type': 'user', 'uuid': 'fg-result', 'sessionId': NATIVE, 'cwd': workspace, 'isSidechain': False,
              'sourceToolAssistantUUID': 'fg-use', 'toolUseResult': completed_result('child-1', 'bounded task'),
              'message': {'role': 'user', 'content': [
                  {'type': 'tool_result', 'tool_use_id': 'toolu_fg_launch', 'content': 'Agent finished.'}]}}
    return [use, result]


def failed_launch_rows(workspace):
    use = {'type': 'assistant', 'uuid': 'failed-use', 'sessionId': NATIVE, 'cwd': workspace, 'isSidechain': False,
           'message': {'role': 'assistant', 'content': [
               {'type': 'tool_use', 'id': 'toolu_failed_launch', 'name': 'Agent',
                'input': {'prompt': 'Investigate the failure.'}}]}}
    result = {'type': 'user', 'uuid': 'failed-result', 'sessionId': NATIVE, 'cwd': workspace, 'isSidechain': False,
              'sourceToolAssistantUUID': 'failed-use', 'toolUseResult': {'status': 'failed', 'agentId': 'child-2'},
              'message': {'role': 'user', 'content': [
                  {'type': 'tool_result', 'tool_use_id': 'toolu_failed_launch', 'content': 'Agent failed.',
                   'is_error': True}]}}
    return [use, result]


def background_launch_rows(workspace):
    use = {'type': 'assistant', 'uuid': 'bg-use', 'sessionId': NATIVE, 'cwd': workspace, 'isSidechain': False,
           'message': {'role': 'assistant', 'content': [
               {'type': 'tool_use', 'id': 'toolu_bg_launch', 'name': 'Task',
                'input': {'prompt': 'Run the long background job.', 'run_in_background': True}}]}}
    ack = {'type': 'user', 'uuid': 'bg-ack', 'sessionId': NATIVE, 'cwd': workspace, 'isSidechain': False,
           'sourceToolAssistantUUID': 'bg-use', 'toolUseResult': {'agentId': 'child-3'},
           'message': {'role': 'user', 'content': [
               {'type': 'tool_result', 'tool_use_id': 'toolu_bg_launch', 'content': 'Task started in background.'}]}}
    notification_text = (
        '<task-notification>\n'
        '<task-id>child-3</task-id>\n'
        '<tool-use-id>toolu_bg_launch</tool-use-id>\n'
        '<output-file>/synthetic/output/bg-task.txt</output-file>\n'
        '<status>completed</status>\n'
        '<summary>Background task finished.</summary>\n'
        '<note>none</note>\n'
        '</task-notification>'
    )
    notification = {'type': 'user', 'uuid': 'bg-notification', 'sessionId': NATIVE, 'cwd': workspace,
                    'isSidechain': False, 'origin': {'kind': 'task-notification'}, 'promptSource': 'sdk',
                    'queueSkipAttachments': True, 'userType': 'external', 'entrypoint': 'sdk-ts',
                    'message': {'role': 'user', 'content': notification_text}}
    return [use, ack, notification]


def _set_path(container, path, value):
    for key in path[:-1]:
        container = container[key]
    container[path[-1]] = value


def exact_length_line(row, target_bytes, pad_field=('message', 'content', 0, 'input', 'file_path')):
    """Return one JSONL line (bytes, trailing newline included) whose content, excluding the
    newline, is exactly target_bytes long, by growing or shrinking the string at pad_field."""
    probe = copy.deepcopy(row)
    _set_path(probe, pad_field, '')
    base_len = len(_line(probe)) - 1
    pad_len = target_bytes - base_len
    if pad_len < 0:
        raise ValueError('target_bytes %d too small for this row shape (%d bytes unpadded)'
                          % (target_bytes, base_len))
    _set_path(probe, pad_field, 'X' * pad_len)
    line = _line(probe)
    assert len(line) - 1 == target_bytes, (len(line) - 1, target_bytes)
    return line


def _fast_progress_rows(count, start=1):
    """Yield `count` minimal, valid JSONL progress rows without per-row json.dumps, for speed
    (benchmarked ~0.45s for 2,000,000 rows vs ~3.8s via json.dumps); record COUNT, not byte shape,
    is what P9 needs exact."""
    template = '{"type": "progress", "uuid": "p-%d"}\n'
    for i in range(start, start + count):
        yield (template % i).encode('ascii')


def write_record_count_fixture(path, total_records, anchor, stop):
    """Write exactly total_records JSONL lines to path: anchor, stop, then (total_records - 2)
    fast padding rows."""
    padding_needed = total_records - 2
    if padding_needed < 0:
        raise ValueError('total_records must be at least 2 (anchor + stop)')
    with open(path, 'wb') as fh:
        fh.write(_line(anchor))
        fh.write(_line(stop))
        batch = []
        for row_bytes in _fast_progress_rows(padding_needed):
            batch.append(row_bytes)
            if len(batch) >= 50_000:
                fh.write(b''.join(batch))
                batch = []
        if batch:
            fh.write(b''.join(batch))


# ---------------------------------------------------------------------------------------------
# P1 (a, b, c, e) + P7: ao_native_outcome.inspect on a >64,000,000 byte history;
# ao_progress._window regression guard (never refuses on size, unchanged).
# ---------------------------------------------------------------------------------------------

class NativeOutcomeCapacityTests(unittest.TestCase):
    """P1(a,b,c,e): ao_native_outcome.inspect on a >64,000,000 byte history, dual-observation design.

    Post-repair, inspect() must succeed on this giant fixture and match an identical-content small
    control on every content-derived field, for every scenario, regardless of byte position. On this
    unchanged baseline, inspect()'s size check (before.st_size > 64_000_000) still fires first for
    every one of them, so each giant assertion below is RED for that one capacity reason. Also carries
    P7: ao_progress's bounded-tail reader, which must keep working on the same oversized fixture,
    unchanged; and the headline forward-repair-constants proof.
    """

    POSITION = {'a': ('EARLY', 2_000_000), 'b': ('MIDDLE', 25_000_000),
                'e': ('MIDDLE', 45_000_000), 'c': ('LATE', 65_000_000)}
    ORDER = ('a', 'b', 'e', 'c')

    @classmethod
    def setUpClass(cls):
        cls.giant = TransitionCase('setUp')
        cls.giant.setUp()
        cls.giant_state = cls.giant.build_room()
        cls.workspace = str(cls.giant.repo)
        base_request = cls.giant_state['requests']['spec_review']
        cls.requests, cls.offsets = {}, {}
        t0 = time.time()
        index = 0
        with open(cls.giant.transcript, 'wb') as fh:
            for order, key in enumerate(cls.ORDER):
                _, target = cls.POSITION[key]
                index, _written = write_padding_bytes(fh, NATIVE, cls.workspace, target - fh.tell(), index)
                cls.offsets[key] = fh.tell()
                text = 'Review the exact synthetic specification for turn %s.' % key
                request = copy.deepcopy(base_request)
                request.update(request_id='turn_' + key, purpose='turn_' + key, text=text,
                                text_sha256=ao.digest(text.encode()), created_at=BASE_T + order * 4000.0)
                cls.requests[key] = request
                for row in scenario_rows(key, request, NATIVE, cls.workspace):
                    fh.write(_line(row))
        cls.generation_seconds = time.time() - t0
        cls.giant_size = cls.giant.transcript.stat().st_size
        cls.source = {'database': str(cls.giant.database), 'transcript': str(cls.giant.transcript),
                       'session_id': SESSION, 'native_session_id': NATIVE}
        cls.snapshot = cls.giant.fake.snapshots[SESSION]

        cls.small = TransitionCase('setUp')
        cls.small.setUp()
        cls.small_state = cls.small.build_room()
        cls.small_workspace = str(cls.small.repo)
        small_base_request = cls.small_state['requests']['spec_review']
        cls.small_requests = {}
        # The control is the SAME four-scenario sequence (a, b, e, c), written back-to-back with no
        # padding at all -- not one scenario in isolation. events_outcome's next_human_uuid is the
        # next human-authored row anywhere in the session, ordered by timestamp, so a single-scenario
        # control (one human row, no successor) can never match this giant fixture, where scenario
        # a's successor is scenario b's anchor and scenario e's successor is scenario c's anchor.
        # Reusing the giant's own per-key created_at offset (BASE_T + order * 4000.0) keeps the two
        # sequences' relative ordering identical, so every scenario's next_human_uuid (including its
        # absence for 'c', last in ORDER in both fixtures) matches between giant and control.
        with open(cls.small.transcript, 'wb') as fh:
            for order, key in enumerate(cls.ORDER):
                text = 'Review the exact synthetic specification for turn %s.' % key
                request = copy.deepcopy(small_base_request)
                request.update(request_id='turn_' + key, purpose='turn_' + key, text=text,
                                text_sha256=ao.digest(text.encode()), created_at=BASE_T + order * 4000.0)
                cls.small_requests[key] = request
                for row in scenario_rows(key, request, NATIVE, cls.small_workspace):
                    fh.write(_line(row))
        cls.small_source = {'database': str(cls.small.database), 'transcript': str(cls.small.transcript),
                             'session_id': SESSION, 'native_session_id': NATIVE}
        cls.small_snapshot = cls.small.fake.snapshots[SESSION]
        cls.small_results = {key: ano.inspect(cls.small.directory, cls.small_state, cls.small_requests[key],
                                              cls.small_source, cls.small_snapshot) for key in cls.ORDER}

    @classmethod
    def tearDownClass(cls):
        # LIFO requirement (module docstring has the full mechanism): cls.small's four patches
        # started while cls.giant's were already active, so mock.patch.object.stop() must undo them
        # in the strict reverse of that setup order -- cls.small first, cls.giant last -- or
        # cls.small's own .stop() call ends up "restoring" each attribute to cls.giant's stub instead
        # of the real one, leaking it for the rest of this process. tearDownModule() below asserts
        # this never happens again.
        cls.small.doCleanups()
        cls.giant.doCleanups()

    def _small_result(self, key):
        return self.small_results[key]

    def _assert_matches_control(self, result, control):
        """Every key of inspect()'s result is content-derived and must match the control exactly,
        except 'source' (embeds the transcript path -- the giant and small fixtures are independent
        TransitionCase worktrees) and 'source_sha256' (the digest of that whole, differently-sized,
        differently-padded file); compaction_failure's own nested 'workspace_sha256' is excluded for
        the same independent-worktree reason.
        """
        excluded = {'source', 'source_sha256'}
        self.assertEqual(set(result) - excluded, set(control) - excluded)
        for key in control:
            if key in excluded:
                continue
            if key == 'compaction_failure':
                giant_proof = {k: v for k, v in result['compaction_failure'].items() if k != 'workspace_sha256'}
                small_proof = {k: v for k, v in control['compaction_failure'].items() if k != 'workspace_sha256'}
                self.assertEqual(giant_proof, small_proof)
                continue
            self.assertEqual(result[key], control[key], key)

    def test_p1_scenario_a_clean_completed_turn(self):
        control = self._small_result('a')
        self.assertEqual(control['errors'], [])  # control: passes on baseline and after repair
        self.assertEqual(control['stop_reasons'], ['end_turn'])
        # b is a's successor in ORDER, in both the giant and the (now multi-scenario) control.
        self.assertEqual(control['next_human_uuid'], 'turn-b-anchor')
        label, _ = self.POSITION['a']
        with self.subTest(scenario='a', position=label, anchor_offset=self.offsets['a'],
                           giant_size=self.giant_size, generation_seconds=round(self.generation_seconds, 3)):
            result = ano.inspect(self.giant.directory, self.giant_state, self.requests['a'], self.source,
                                  self.snapshot)
            self.assertEqual(result['source_sha256'],
                              hashlib.sha256(self.giant.transcript.read_bytes()).hexdigest())
            self.assertEqual(result['next_human_uuid'], 'turn-b-anchor')
            self._assert_matches_control(result, control)

    def test_p1_scenario_b_quota_failure_via_classify(self):
        control = self._small_result('b')
        self.assertEqual(control['errors'],  # control: passes on baseline and after repair
                          [{'uuid': 'turn-b-error', 'error': 'rate_limit', 'http_status': 429}])
        # e is b's successor in ORDER, in both the giant and the (now multi-scenario) control.
        self.assertEqual(control['next_human_uuid'], 'turn-e-anchor')
        label, _ = self.POSITION['b']
        with self.subTest(scenario='b', position=label, anchor_offset=self.offsets['b'], giant_size=self.giant_size):
            result = ano.inspect(self.giant.directory, self.giant_state, self.requests['b'], self.source,
                                  self.snapshot)
            self.assertEqual(result['source_sha256'],
                              hashlib.sha256(self.giant.transcript.read_bytes()).hexdigest())
            self.assertEqual(result['next_human_uuid'], 'turn-e-anchor')
            self._assert_matches_control(result, control)
            receipt = {'turn': {'id': 'turn-1', 'state': 'completed', 'providerTurnId': 'provider-1'},
                       'messages': [{'id': 'm-1', 'role': 'assistant', 'text': ''}], 'history_truncated': False}
            classification = ao_outcomes.classify(receipt, native=result)
            self.assertEqual(classification['kind'], 'quota_limit')
            self.assertTrue(classification['hold'])

    def test_p1_scenario_c_autocompact_thrashing(self):
        control = self._small_result('c')
        self.assertIsNotNone(control.get('compaction_failure'))  # control: passes on baseline and after repair
        self.assertEqual(control['compaction_failure']['kind'], 'autocompact_thrashing')
        # c is last in ORDER, in both the giant and the (now multi-scenario) control: no successor.
        self.assertIsNone(control['next_human_uuid'])
        label, _ = self.POSITION['c']
        with self.subTest(scenario='c', position=label, anchor_offset=self.offsets['c'], giant_size=self.giant_size):
            result = ano.inspect(self.giant.directory, self.giant_state, self.requests['c'], self.source,
                                  self.snapshot)
            self.assertEqual(result['source_sha256'],
                              hashlib.sha256(self.giant.transcript.read_bytes()).hexdigest())
            self.assertIsNotNone(result.get('compaction_failure'))
            self.assertIsNone(result['next_human_uuid'])
            self._assert_matches_control(result, control)

    def test_p1_scenario_e_incomplete_turn(self):
        control = self._small_result('e')
        self.assertEqual(control['errors'], [])  # control: passes on baseline and after repair
        self.assertEqual(control['stop_reasons'], ['tool_use'])
        # c is e's successor in ORDER, in both the giant and the (now multi-scenario) control.
        self.assertEqual(control['next_human_uuid'], 'turn-c-anchor')
        label, _ = self.POSITION['e']
        with self.subTest(scenario='e', position=label, anchor_offset=self.offsets['e'], giant_size=self.giant_size):
            result = ano.inspect(self.giant.directory, self.giant_state, self.requests['e'], self.source,
                                  self.snapshot)
            self.assertEqual(result['source_sha256'],
                              hashlib.sha256(self.giant.transcript.read_bytes()).hexdigest())
            self.assertEqual(result['next_human_uuid'], 'turn-c-anchor')
            self._assert_matches_control(result, control)

    def test_p7_ao_progress_window_never_refuses_on_size(self):
        """Regression guard: ao_progress._window must keep reading a bounded tail, unchanged."""
        # control: passes on baseline and after repair
        rows, malformed = ao_progress._window(str(self.giant.transcript), NATIVE)
        self.assertEqual(malformed, 0)
        self.assertTrue(any(row.get('uuid') == 'turn-c-error' for row in rows))

    def test_forward_repair_constants_match_the_post_repair_contract(self):
        # Headline proof: the shared byte cap itself must move to 256,000,000 on both modules, and
        # the new per-record/per-count constants and string refusals must exist. On this baseline,
        # ano.MAX_TRANSCRIPT_BYTES does not exist (None) and et.MAX_TRANSCRIPT_BYTES is still
        # 64_000_000, so both comparisons below fail cleanly for the capacity reason.
        self.assertEqual(getattr(ano, 'MAX_TRANSCRIPT_BYTES', None), 256_000_000)
        self.assertEqual(getattr(et, 'MAX_TRANSCRIPT_BYTES', None), 256_000_000)
        self.assertEqual(getattr(ano, 'MAX_TRANSCRIPT_RECORD_BYTES', None), 16_777_216)
        self.assertEqual(getattr(ano, 'MAX_TRANSCRIPT_RECORDS', None), 2_000_000)
        for name in ('TRANSCRIPT_RECORD_BYTES_REFUSAL', 'TRANSCRIPT_RECORD_COUNT_REFUSAL',
                     'TRANSCRIPT_MALFORMED_REFUSAL'):
            with self.subTest(constant=name):
                self.assertIsInstance(getattr(ano, name, None), str)


# ---------------------------------------------------------------------------------------------
# P1(d) + P2: qualified-family model contradiction; preflight_source/validate_registered_source/
# freeze_request on an existing >64,000,000 byte registered transcript.
# ---------------------------------------------------------------------------------------------

P1D_P2_CHILD_SCRIPT = r'''
import hashlib, json, os, sys
sys.path.insert(0, os.getcwd())
import ao_engineering_model as em
import ao_native_outcome as ano
from room import RoomError
from test_ao_native_transcript_capacity import _line, _iso, write_padding_bytes
from test_ao_normal import Fixture

fx = Fixture('setUp')
fx.setUp()
try:
    fx.room = fx.open()
    fx.spec()
    fx.bind()
    directory = fx.directory()
    fx.send('spec_review')
    state = fx.state()
    request = state['requests']['spec_review']
    workspace = fx.native_row_workspace()
    snapshot = fx.fake.conversation('engineer')
    created_at = request['created_at']
    anchor = {'type': 'user', 'uuid': 'h-anchor-d', 'sessionId': fx.NATIVE, 'cwd': workspace,
              'isSidechain': False, 'timestamp': _iso(created_at + 0.001), 'origin': {'kind': 'human'},
              'message': {'role': 'user', 'content': request['text']}}
    stop = {'type': 'assistant', 'uuid': 'a-wrongmodel', 'sessionId': fx.NATIVE, 'cwd': workspace,
            'isSidechain': False, 'timestamp': _iso(created_at + 0.002),
            'message': {'role': 'assistant', 'model': 'claude-opus-5-5', 'id': 'm-wrong',
                        'stop_reason': 'end_turn',
                        'content': [{'type': 'text', 'text': 'Done (wrong family member).'}]}}
    fx.transcript.write_bytes(_line(anchor) + _line(stop))
    # The small-control observation must happen here, while the registered transcript is still
    # small: preflight_source ties the registered source to this one fixed path/name, so every
    # later call in this script only ever observes the grown (giant) file.
    small_result = ano.inspect(directory, state, request, fx.native_outcome_source, snapshot)
    # Grow the registered transcript past the old 64,000,000 byte cap by APPENDING realistic padding
    # rows of the same native session/workspace, never a human-text row, instead of os.truncate's
    # NUL-fill: a NUL run is one pathologically oversized record (far past the new 16,777,216-byte
    # per-record budget) that the record-budget check refuses on its own, independent of (and
    # masking) the whole-file capacity repair this script actually proves. These padding rows carry
    # a timestamp far before this turn's anchor (see write_padding_bytes/padding_pair), so
    # next_human_uuid and the owned turn's model_contradiction stay exactly as the small control
    # observed them.
    with open(fx.transcript, 'ab') as fh:
        write_padding_bytes(fh, fx.NATIVE, workspace, 70_000_000 - fx.transcript.stat().st_size, 0)

    out = {'model_contradiction': small_result.get('model_contradiction'),
           'unknown': small_result.get('unknown'), 'giant_size': fx.transcript.stat().st_size,
           'giant_sha256': hashlib.sha256(fx.transcript.read_bytes()).hexdigest()}

    try:
        giant_result = ano.inspect(directory, state, request, fx.native_outcome_source, snapshot)
        out['inspect_error'] = None
        out['giant_model_contradiction'] = giant_result.get('model_contradiction')
        out['giant_source_sha256'] = giant_result.get('source_sha256')
    except RoomError as exc:
        out['inspect_error'] = str(exc)
        out['giant_model_contradiction'] = None
        out['giant_source_sha256'] = None

    for key, call in (
        ('freeze_error', lambda: em.freeze_request(directory, state)),
        ('validate_error', lambda: ano.validate_registered_source(directory, state, 'engineer')),
    ):
        try:
            call()
            out[key] = None
        except RoomError as exc:
            out[key] = str(exc)
finally:
    fx.doCleanups()

sys.stdout.write(json.dumps(out) + "\n")
'''


class QualifiedFamilyCapacityTests(unittest.TestCase):
    """P1(d): wrong-model stop row for a qualified family request, dual-observation design.

    P2: ao_engineering_model.freeze_request (which calls _require_qualified_source, which calls
    ao_native_outcome.validate_registered_source with its default 64,000,000 byte limit) on an
    already-registered transcript that has since grown to 70,000,000 bytes -- over today's cap, under
    the repaired 256,000,000 byte cap. Post-repair, every call below on the grown transcript must
    succeed and the inspect() result must match the small control exactly.

    Runs in a child process (P1D_P2_CHILD_SCRIPT, module docstring explains why); the real,
    unmodified production functions are still what gets called, just from a separate process.
    """

    @classmethod
    def setUpClass(cls):
        t0 = time.time()
        env = os.environ.copy()
        existing = env.get('PYTHONPATH')
        env['PYTHONPATH'] = str(MODULE_DIR) + (os.pathsep + existing if existing else '')
        proc = subprocess.run([sys.executable, '-B', '-c', P1D_P2_CHILD_SCRIPT], cwd=str(MODULE_DIR),
                              env=env, capture_output=True, text=True, timeout=120)
        cls.generation_seconds = time.time() - t0
        if proc.returncode != 0:
            raise RuntimeError('P1(d)/P2 child process failed (exit %s):\nstdout:\n%s\nstderr:\n%s'
                                % (proc.returncode, proc.stdout[-4000:], proc.stderr[-4000:]))
        try:
            cls.result = json.loads(proc.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError) as exc:
            raise RuntimeError('P1(d)/P2 child process printed no parseable JSON line (exit %s):\n'
                                'stdout:\n%s\nstderr:\n%s'
                                % (proc.returncode, proc.stdout[-4000:], proc.stderr[-4000:])) from exc
        cls.giant_size = cls.result['giant_size']

    def test_p1_scenario_d_wrong_model_stop_row_small_control(self):
        # control: passes on baseline and after repair (captured while the registered transcript was
        # still small, before the child script grows it to 70,000,000 bytes)
        result = self.result
        self.assertEqual(result['model_contradiction']['expected_model'], 'claude-fable-5-1')
        self.assertEqual(result['model_contradiction']['observed_models'], ['claude-opus-5-5'])
        self.assertIn('Native response model contradicts the qualified expected model', result['unknown'])

    def test_p1_scenario_d_giant_transcript_matches_the_small_control_after_repair(self):
        with self.subTest(giant_size=self.giant_size, generation_seconds=round(self.generation_seconds, 3)):
            self.assertIsNone(self.result['inspect_error'])
            self.assertEqual(self.result['giant_source_sha256'], self.result['giant_sha256'])
            # model_contradiction embeds the observing call's own source_sha256 (ao_native_outcome
            # .inspect stamps result['source_sha256'] into the settled contradiction dict); growing
            # the registered transcript between the small and giant observations legitimately
            # changes that one nested digest, so it is excluded from the content comparison below and
            # asserted explicitly instead, against each observation's own independently-verified
            # transcript digest.
            self.assertEqual(self.result['giant_model_contradiction']['source_sha256'],
                              self.result['giant_source_sha256'])
            giant_contradiction = {k: v for k, v in self.result['giant_model_contradiction'].items()
                                   if k != 'source_sha256'}
            small_contradiction = {k: v for k, v in self.result['model_contradiction'].items()
                                   if k != 'source_sha256'}
            self.assertEqual(giant_contradiction, small_contradiction)

    def test_p2_freeze_request_admits_the_grown_registered_transcript_after_repair(self):
        with self.subTest(giant_size=self.giant_size):
            self.assertIsNone(self.result['freeze_error'])

    def test_p2_validate_registered_source_admits_the_grown_transcript_after_repair(self):
        with self.subTest(giant_size=self.giant_size):
            self.assertIsNone(self.result['validate_error'])


# ---------------------------------------------------------------------------------------------
# P3: ao_engineering_transition._read_transcript + child_evidence.
# ---------------------------------------------------------------------------------------------

class ChildEvidenceCapacityTests(unittest.TestCase):
    """P3: _read_transcript + child_evidence on a >64,000,000 byte history with Agent/Task launches
    at early/mid/late byte positions (foreground completed, failed launch, background with an SDK
    terminal notification).

    Post-repair, _read_transcript returns a dict {'sha256', 'size', 'records', 'events', 'offsets',
    'ends', 'prefix_sha256'} instead of raw bytes, and child_evidence accepts that parsed 'events' list
    directly (child_evidence on raw bytes remains supported too, for the small control below). On
    this unchanged baseline, _read_transcript's ownership/size check still masks every position
    identically before any dict is ever produced, so every giant assertion here is RED for that one
    capacity reason.
    """

    POSITION = {'foreground': ('EARLY', 2_000_000), 'failed': ('MIDDLE', 35_000_000),
                'background': ('LATE', 65_000_000)}
    ORDER = ('foreground', 'failed', 'background')
    BUILDER = {'foreground': foreground_completed_rows, 'failed': failed_launch_rows,
               'background': background_launch_rows}

    @classmethod
    def setUpClass(cls):
        cls.giant = TransitionCase('setUp')
        cls.giant.setUp()
        cls.giant.build_room()
        cls.workspace = str(cls.giant.repo)
        cls.offsets = {}
        index = 0
        t0 = time.time()
        with open(cls.giant.transcript, 'wb') as fh:
            for key in cls.ORDER:
                _, target = cls.POSITION[key]
                index, _written = write_padding_bytes(fh, NATIVE, cls.workspace, target - fh.tell(), index)
                cls.offsets[key] = fh.tell()
                for row in cls.BUILDER[key](cls.workspace):
                    fh.write(_line(row))
        cls.generation_seconds = time.time() - t0
        cls.giant_size = cls.giant.transcript.stat().st_size

        cls.small = TransitionCase('setUp')
        cls.small.setUp()
        cls.small.build_room()
        cls.small_workspace = str(cls.small.repo)
        # The control is the SAME three-launch scenario sequence (foreground, failed, background) as
        # the giant fixture, concatenated with no padding at all -- not one launch in isolation.
        # child_evidence accumulates launches/results/notifications across the whole file it is
        # handed, so a single-scenario control (one launch) can never match this giant fixture, which
        # carries all three. Built via BUILDER directly (not scenario_rows; that helper is P1's), on
        # the small fixture's own independent workspace: none of launches/results/notifications or
        # the returned counts embed a workspace path or string, only tool/row/agent identities fixed
        # by each BUILDER function, so this control is exactly comparable to the giant result.
        cls.small_raw = b''.join(_line(row) for key in cls.ORDER for row in cls.BUILDER[key](cls.small_workspace))
        cls.small_result = et.child_evidence(cls.small_raw, NATIVE, cls.small_workspace)  # control: raw bytes stay supported

    @classmethod
    def tearDownClass(cls):
        # Same LIFO requirement as NativeOutcomeCapacityTests.tearDownClass: cls.small was set up
        # after cls.giant, so it must be cleaned up first, or cls.small's own .stop() call leaks
        # cls.giant's stub as the permanent value of each patched attribute.
        cls.small.doCleanups()
        cls.giant.doCleanups()

    def _giant_reading(self):
        reading = et._read_transcript(str(self.giant.transcript), NATIVE)
        self.assertEqual(set(reading), {'sha256', 'size', 'records', 'events', 'offsets', 'ends', 'prefix_sha256'})
        self.assertEqual(reading['sha256'], hashlib.sha256(self.giant.transcript.read_bytes()).hexdigest())
        self.assertEqual(reading['size'], self.giant_size)
        self.assertIsNone(reading['prefix_sha256'])
        return et.child_evidence(reading['events'], NATIVE, self.workspace)

    def _assert_matches_control(self, result):
        """Every key of child_evidence's result must match the shared three-launch control exactly,
        except 'parent_tool_uses': the giant fixture's own >=65,000,000 bytes of Read-tool padding
        rows (between the three launches) are each one more tool_use the control never sees, so that
        one count legitimately differs and is excluded here; it is not part of the launch/result/
        notification correlation this capacity contract proves.
        """
        control = self.small_result
        excluded = {'parent_tool_uses'}
        self.assertEqual(set(result) - excluded, set(control) - excluded)
        for key in control:
            if key in excluded:
                continue
            self.assertEqual(result[key], control[key], key)

    def test_p3_foreground_completed_launch(self):
        control = self.small_result
        # control: passes on baseline and after repair. Counts are for the full three-launch
        # sequence (foreground + failed + background), not the foreground launch alone.
        self.assertEqual(control['agent_launches'], 3)
        self.assertEqual(control['background_launches'], 1)
        self.assertEqual(control['correlated_results'], 3)
        self.assertEqual(control['terminal_api_errors'], 0)
        self.assertEqual(control['terminal_notifications'], 1)
        label, offset = self.POSITION['foreground'][0], self.offsets['foreground']
        with self.subTest(launch='foreground', position=label, anchor_offset=offset, giant_size=self.giant_size,
                           generation_seconds=round(self.generation_seconds, 3)):
            result = self._giant_reading()
            self._assert_matches_control(result)

    def test_p3_failed_launch(self):
        control = self.small_result
        # control: passes on baseline and after repair. Counts are for the full three-launch
        # sequence (foreground + failed + background), not the failed launch alone -- in particular,
        # terminal_notifications is 1 here (the background launch's), not 0.
        self.assertEqual(control['agent_launches'], 3)
        self.assertEqual(control['correlated_results'], 3)
        self.assertEqual(control['terminal_notifications'], 1)
        label, offset = self.POSITION['failed'][0], self.offsets['failed']
        with self.subTest(launch='failed', position=label, anchor_offset=offset, giant_size=self.giant_size):
            result = self._giant_reading()
            self._assert_matches_control(result)

    def test_p3_background_launch_with_sdk_terminal_notification(self):
        control = self.small_result
        # control: passes on baseline and after repair. Counts are for the full three-launch
        # sequence (foreground + failed + background), not the background launch alone.
        self.assertEqual(control['agent_launches'], 3)
        self.assertEqual(control['background_launches'], 1)
        self.assertEqual(control['terminal_notifications'], 1)
        label, offset = self.POSITION['background'][0], self.offsets['background']
        with self.subTest(launch='background', position=label, anchor_offset=offset, giant_size=self.giant_size):
            result = self._giant_reading()
            self._assert_matches_control(result)


# ---------------------------------------------------------------------------------------------
# P4: identity/ownership checks shared by _read_transcript and inspect, independent of the size cap.
# ---------------------------------------------------------------------------------------------

class TranscriptIdentityCapacityTests(unittest.TestCase):
    """P4: mode, symlink, FIFO, wrong filename and foreign-session identity checks. None of these
    depend on file size, so all but one are controls, unaffected by the capacity repair. The one
    genuine repair is the directory-at-path case: today, os.fdopen(fd, 'rb') on a directory raises a
    raw IsADirectoryError immediately (confirmed: before either lane's own stat-based checks ever
    run, on both lanes identically), instead of each lane's own clean refusal.
    """

    def setUp(self):
        self.tc = TransitionCase('setUp')
        self.tc.setUp()
        self.state = self.tc.build_room()
        self.workspace = str(self.tc.repo)

    def tearDown(self):
        self.tc.doCleanups()

    def _write(self, path=None):
        path = path or self.tc.transcript
        row = {'uuid': 'h', 'type': 'user', 'sessionId': NATIVE, 'cwd': self.workspace,
               'message': {'role': 'user', 'content': TEXT}}
        path.write_bytes(_line(row))
        return row

    def test_p4_mode_0o644_is_accepted_not_group_or_other_writable(self):
        # control: passes on baseline and after repair (mode logic is unrelated to the byte cap).
        # 0o644 (rw-r--r--) carries no group/other WRITE bit (0o644 & 0o022 == 0); _read_transcript's
        # check is specifically `before.st_mode & 0o022`, so this exact mode is accepted, not refused.
        self._write()
        os.chmod(self.tc.transcript, 0o644)
        raw = et._read_transcript(str(self.tc.transcript), NATIVE)
        self.assertTrue(raw)

    def test_p4_group_writable_mode_0o664_is_refused(self):
        self._write()  # control: passes on baseline and after repair
        os.chmod(self.tc.transcript, 0o664)
        with self.assertRaises(RoomError) as ctx:
            et._read_transcript(str(self.tc.transcript), NATIVE)
        self.assertEqual(str(ctx.exception), 'Native transcript ownership, type or size is unsafe')

    def test_p4_symlinked_parent_directory_is_refused(self):
        real_dir = self.tc.root / 'real_parent'  # control: passes on baseline and after repair
        real_dir.mkdir()
        self._write(real_dir / (NATIVE + '.jsonl'))
        link_dir = self.tc.root / 'link_parent'
        link_dir.symlink_to(real_dir)
        with self.assertRaises(RoomError) as ctx:
            et._read_transcript(str(link_dir / (NATIVE + '.jsonl')), NATIVE)
        self.assertEqual(str(ctx.exception), 'Use the exact owned native transcript without symlinks; its file '
                                              'name must be the retained provider session identity plus .jsonl')

    def test_p4_fifo_at_path_is_refused(self):
        if self.tc.transcript.exists():  # control: passes on baseline and after repair
            self.tc.transcript.unlink()
        os.mkfifo(self.tc.transcript)
        try:
            with self.assertRaises(RoomError) as ctx:
                et._read_transcript(str(self.tc.transcript), NATIVE)
            self.assertEqual(str(ctx.exception), 'Native transcript ownership, type or size is unsafe')
        finally:
            self.tc.transcript.unlink()

    def test_p4_directory_at_path_is_refused_cleanly_in_both_lanes(self):
        # Repaired: fail-closed identity. Today, os.fdopen(fd, 'rb') raises a raw IsADirectoryError
        # for a directory immediately on both lanes (confirmed empirically: fdopen itself raises,
        # not a lazy read, before either lane's own stat-based checks ever run); the repair must map
        # that into each lane's own clean refusal instead of leaking the raw OSError.
        if self.tc.transcript.exists():
            self.tc.transcript.unlink()
        self.tc.transcript.mkdir()
        try:
            with self.subTest(lane='transition'):
                with self.assertRaises(RoomError) as ctx:
                    et._read_transcript(str(self.tc.transcript), NATIVE)
                self.assertEqual(str(ctx.exception), 'Native transcript ownership, type or size is unsafe')
            with self.subTest(lane='inspect'):
                request = self.state['requests']['spec_review']
                source = {'database': str(self.tc.database), 'transcript': str(self.tc.transcript),
                          'session_id': SESSION, 'native_session_id': NATIVE}
                snapshot = self.tc.fake.snapshots[SESSION]
                with self.assertRaises(RoomError) as ctx:
                    ano.inspect(self.tc.directory, self.state, request, source, snapshot)
                self.assertEqual(str(ctx.exception), 'Native transcript ownership or size is unsafe')
        finally:
            self.tc.transcript.rmdir()

    def test_p4_wrong_filename_is_refused(self):
        wrong = self.tc.root / 'wrong-name.jsonl'  # control: passes on baseline and after repair
        self._write(wrong)
        with self.assertRaises(RoomError) as ctx:
            et._read_transcript(str(wrong), NATIVE)
        self.assertEqual(str(ctx.exception), 'Use the exact owned native transcript without symlinks; its file '
                                              'name must be the retained provider session identity plus .jsonl')

    def test_p4_foreign_session_id_fails_anchor_correlation(self):
        request = self.state['requests']['spec_review']  # control: passes on baseline and after repair
        anchor = {'type': 'user', 'uuid': 'h-anchor', 'sessionId': 'some-other-session', 'cwd': self.workspace,
                  'isSidechain': False, 'timestamp': _iso(request['created_at'] + 0.001),
                  'origin': {'kind': 'human'}, 'message': {'role': 'user', 'content': request['text']}}
        stop = {'type': 'assistant', 'uuid': 'a-reply', 'sessionId': NATIVE, 'cwd': self.workspace,
                'isSidechain': False, 'timestamp': _iso(request['created_at'] + 0.002),
                'message': {'role': 'assistant', 'model': FABLE, 'id': 'm0', 'stop_reason': 'end_turn',
                            'content': [{'type': 'text', 'text': 'Synthetic completed answer.'}]}}
        self.tc.transcript.write_bytes(_line(anchor) + _line(stop))
        source = {'database': str(self.tc.database), 'transcript': str(self.tc.transcript),
                  'session_id': SESSION, 'native_session_id': NATIVE}
        snapshot = self.tc.fake.snapshots[SESSION]
        with self.assertRaises(RoomError) as ctx:
            ano.inspect(self.tc.directory, self.state, request, source, snapshot)
        self.assertEqual(str(ctx.exception), 'Native caller correlation is missing or ambiguous')

    def test_p4_getuid_mismatch_refuses_both_lanes_small_control(self):
        # F6a control: passes on baseline and after repair -- uid ownership logic predates and is
        # unrelated to the byte-cap repair. See MutationOnGiantTranscriptCapacityTests for the
        # giant-scale counterpart and why inspect's own transcript-ownership message is unreachable
        # from a process-wide uid mismatch (ao_native_outcome.validate_source's earlier owner-
        # database ownership check fires first, identically at both scales).
        self._write()
        request = self.state['requests']['spec_review']
        source = {'database': str(self.tc.database), 'transcript': str(self.tc.transcript),
                  'session_id': SESSION, 'native_session_id': NATIVE}
        snapshot = self.tc.fake.snapshots[SESSION]
        with mock.patch('os.getuid', return_value=os.getuid() + 1):
            with self.assertRaises(RoomError) as ctx:
                ano.inspect(self.tc.directory, self.state, request, source, snapshot)
            self.assertEqual(str(ctx.exception), 'Cannot establish the retained native outcome owner')
            with self.assertRaises(RoomError) as ctx:
                et._read_transcript(str(self.tc.transcript), NATIVE)
            self.assertEqual(str(ctx.exception), 'Native transcript ownership, type or size is unsafe')


# ---------------------------------------------------------------------------------------------
# P5: mutation between two observations (inspect lane, small scale -- control).
# ---------------------------------------------------------------------------------------------

class InspectMutationCapacityTests(unittest.TestCase):
    """P5 (inspect lane, small scale): mutation between two observations changes the observed source
    digest, except a truncation that removes the anchor, which refuses correlation instead. Pure
    content/correlation logic, unrelated to the byte cap: a control at this scale, unaffected by the
    capacity repair. See MutationOnGiantTranscriptCapacityTests for the giant-scale counterpart, which
    the capacity repair does bear on.
    """

    @classmethod
    def setUpClass(cls):
        cls.tc = TransitionCase('setUp')
        cls.tc.setUp()
        cls.state = cls.tc.build_room()
        cls.request = cls.state['requests']['spec_review']
        cls.workspace = str(cls.tc.repo)
        cls.source = {'database': str(cls.tc.database), 'transcript': str(cls.tc.transcript),
                       'session_id': SESSION, 'native_session_id': NATIVE}
        cls.snapshot = cls.tc.fake.snapshots[SESSION]
        created_at = cls.request['created_at']
        cls.anchor = {'type': 'user', 'uuid': 'h-anchor', 'sessionId': NATIVE, 'cwd': cls.workspace,
                      'isSidechain': False, 'timestamp': _iso(created_at + 0.001), 'origin': {'kind': 'human'},
                      'message': {'role': 'user', 'content': cls.request['text']}}
        cls.stop = {'type': 'assistant', 'uuid': 'a-reply', 'sessionId': NATIVE, 'cwd': cls.workspace,
                    'isSidechain': False, 'timestamp': _iso(created_at + 0.002),
                    'message': {'role': 'assistant', 'model': FABLE, 'id': 'm0', 'stop_reason': 'end_turn',
                                'content': [{'type': 'text', 'text': 'Synthetic completed answer.'}]}}
        cls.base_bytes = _line(cls.anchor) + _line(cls.stop)

    @classmethod
    def tearDownClass(cls):
        cls.tc.doCleanups()

    def _observe(self):
        return ano.inspect(self.tc.directory, self.state, self.request, self.source, self.snapshot)

    def test_p5_mutation_kinds(self):
        # control: passes on baseline and after repair
        self.tc.transcript.write_bytes(self.base_bytes)
        baseline = self._observe()

        with self.subTest(mutation='appended_row'):
            pad = padding_pair(NATIVE, self.workspace, 1)
            self.tc.transcript.write_bytes(self.base_bytes + pad)
            appended = self._observe()
            self.assertNotEqual(appended['source_sha256'], baseline['source_sha256'])
            self.assertEqual(appended['errors'], baseline['errors'])
            self.assertEqual(appended['stop_reasons'], baseline['stop_reasons'])

        with self.subTest(mutation='same_size_edit'):
            mutated = self.base_bytes.replace(b'Synthetic completed answer.', b'Synthetic completed answer!')
            self.assertEqual(len(mutated), len(self.base_bytes))
            self.assertNotEqual(mutated, self.base_bytes)
            self.tc.transcript.write_bytes(mutated)
            same_size = self._observe()
            self.assertNotEqual(same_size['source_sha256'], baseline['source_sha256'])

        with self.subTest(mutation='full_replacement'):
            replaced_stop = dict(self.stop)
            replaced_stop['message'] = {**self.stop['message'],
                                        'content': [{'type': 'text', 'text': 'Synthetic replaced answer.'}]}
            replaced = _line(self.anchor) + _line(replaced_stop)
            self.tc.transcript.write_bytes(replaced)
            full_replacement = self._observe()
            self.assertNotEqual(full_replacement['source_sha256'], baseline['source_sha256'])

        with self.subTest(mutation='truncation_removes_anchor'):
            stop_first = _line(self.stop) + _line(self.anchor)
            truncated = stop_first[:len(_line(self.stop))]
            self.tc.transcript.write_bytes(truncated)
            with self.assertRaises(RoomError) as ctx:
                self._observe()
            self.assertEqual(str(ctx.exception), 'Native caller correlation is missing or ambiguous')


# ---------------------------------------------------------------------------------------------
# P5: mutation between two observations (child_evidence lane, small scale -- control).
# ---------------------------------------------------------------------------------------------

class ChildEvidenceMutationCapacityTests(unittest.TestCase):
    """P5 (child_evidence lane, small scale): mutation between two observations changes the raw
    observed bytes' digest, except a truncation that drops a launch's correlated result, which
    refuses the transition instead. A control at this scale, unaffected by the capacity repair; see
    MutationOnGiantTranscriptCapacityTests for the giant-scale counterpart.
    """

    @classmethod
    def setUpClass(cls):
        cls.tc = TransitionCase('setUp')
        cls.tc.setUp()
        cls.tc.build_room()
        cls.workspace = str(cls.tc.repo)
        cls.launch_use = {'type': 'assistant', 'uuid': 'launch-1', 'sessionId': NATIVE, 'cwd': cls.workspace,
                          'isSidechain': False, 'message': {'role': 'assistant', 'id': 'm-launch', 'content': [
                              {'type': 'tool_use', 'id': 'toolu_agent1', 'name': 'Agent',
                               'input': {'prompt': 'do work'}}]}}
        cls.launch_result = {'type': 'user', 'uuid': 'result-1', 'sessionId': NATIVE, 'cwd': cls.workspace,
                             'isSidechain': False, 'sourceToolAssistantUUID': 'launch-1',
                             'message': {'role': 'user', 'content': [
                                 {'type': 'tool_result', 'tool_use_id': 'toolu_agent1', 'content': 'Done'}]},
                             'toolUseResult': completed_result('child-agent-1', 'do work')}
        cls.base_bytes = _line(cls.launch_use) + _line(cls.launch_result)

    @classmethod
    def tearDownClass(cls):
        cls.tc.doCleanups()

    def test_p5_mutation_kinds(self):
        # control: passes on baseline and after repair
        baseline = et.child_evidence(self.base_bytes, NATIVE, self.workspace)
        baseline_digest = ao.digest(self.base_bytes)

        with self.subTest(mutation='appended_row'):
            pad = padding_pair(NATIVE, self.workspace, 1)
            appended_bytes = self.base_bytes + pad
            appended = et.child_evidence(appended_bytes, NATIVE, self.workspace)
            self.assertNotEqual(ao.digest(appended_bytes), baseline_digest)
            self.assertEqual(appended['agent_launches'], baseline['agent_launches'])
            self.assertEqual(appended['launches_sha256'], baseline['launches_sha256'])
            self.assertNotEqual(appended['parent_tool_uses'], baseline['parent_tool_uses'])

        with self.subTest(mutation='same_size_edit'):
            mutated_bytes = self.base_bytes.replace(b'"content": "Done"', b'"content": "Dine"')
            self.assertEqual(len(mutated_bytes), len(self.base_bytes))
            self.assertNotEqual(mutated_bytes, self.base_bytes)
            mutated = et.child_evidence(mutated_bytes, NATIVE, self.workspace)
            self.assertNotEqual(ao.digest(mutated_bytes), baseline_digest)
            self.assertEqual(mutated['agent_launches'], baseline['agent_launches'])

        with self.subTest(mutation='full_replacement'):
            replaced_result = dict(self.launch_result)
            replaced_result['message'] = {**self.launch_result['message'], 'content': [
                {'type': 'tool_result', 'tool_use_id': 'toolu_agent1', 'content': 'Finished differently'}]}
            replaced_result['toolUseResult'] = completed_result('child-agent-1', 'do work')
            replaced_bytes = _line(self.launch_use) + _line(replaced_result)
            et.child_evidence(replaced_bytes, NATIVE, self.workspace)
            self.assertNotEqual(ao.digest(replaced_bytes), baseline_digest)

        with self.subTest(mutation='truncation_drops_correlated_result'):
            truncated_bytes = _line(self.launch_use)
            with self.assertRaises(RoomError) as ctx:
                et.child_evidence(truncated_bytes, NATIVE, self.workspace)
            self.assertEqual(str(ctx.exception), 'A parent Agent/Task launch in the retained transcript has no '
                                                  'correlated result; an unfinished child refuses the transition')


# ---------------------------------------------------------------------------------------------
# P5 (giant scale): mutation between two observations of a >64,000,000 byte history.
# ---------------------------------------------------------------------------------------------

class MutationOnGiantTranscriptCapacityTests(unittest.TestCase):
    """P5 (giant scale): mutating a >64,000,000 byte history between two observations changes the
    observed digest in both lanes, exactly like the small-scale P5 controls above, but at the scale
    where today's byte cap would otherwise mask everything. Self-contained: builds and owns its own
    giant fixture per test (setUp/tearDown) so it never shares mutable state with any other TestCase,
    or between any of its own test methods, in this module.

    Also carries F6a (uid ownership, previously untested at giant scale) and F6c (replacement,
    truncation and same-size in-place mutation at giant scale, in both lanes; append was previously
    the only giant-scale mutation kind proven). Each test method below still builds (or, where
    setUp()'s own padding/anchor/stop shape already fits, reuses unmodified) its own fixture content
    inside this one class's per-test setUp(), and never shares mutated bytes with any other test
    method.
    """

    def setUp(self):
        self.tc = TransitionCase('setUp')
        self.tc.setUp()
        self.state = self.tc.build_room()
        self.request = self.state['requests']['spec_review']
        self.workspace = str(self.tc.repo)
        self.source = {'database': str(self.tc.database), 'transcript': str(self.tc.transcript),
                       'session_id': SESSION, 'native_session_id': NATIVE}
        self.snapshot = self.tc.fake.snapshots[SESSION]
        created_at = self.request['created_at']
        self.anchor = {'type': 'user', 'uuid': 'h-anchor', 'sessionId': NATIVE, 'cwd': self.workspace,
                       'isSidechain': False, 'timestamp': _iso(created_at + 0.001), 'origin': {'kind': 'human'},
                       'message': {'role': 'user', 'content': self.request['text']}}
        self.stop = {'type': 'assistant', 'uuid': 'a-reply', 'sessionId': NATIVE, 'cwd': self.workspace,
                     'isSidechain': False, 'timestamp': _iso(created_at + 0.002),
                     'message': {'role': 'assistant', 'model': FABLE, 'id': 'm0', 'stop_reason': 'end_turn',
                                 'content': [{'type': 'text', 'text': 'Synthetic completed answer.'}]}}
        t0 = time.time()
        with open(self.tc.transcript, 'wb') as fh:
            write_padding_bytes(fh, NATIVE, self.workspace, 65_000_000, 0)
            fh.write(_line(self.anchor))
            fh.write(_line(self.stop))
        self.generation_seconds = time.time() - t0
        self.giant_size = self.tc.transcript.stat().st_size

    def tearDown(self):
        self.tc.doCleanups()

    def _replacement_copy(self):
        """A different, valid, same-size giant file: a byte-for-byte copy of the current transcript
        with one character flipped inside an early padding row's own content field -- cheapest way
        to build a second >64,000,000 byte file that still parses cleanly, for an os.replace proof."""
        data = bytearray(self.tc.transcript.read_bytes())
        pos = data.index(b'Synthetic padding content') + 5
        data[pos] = ord('Z') if data[pos] != ord('Z') else ord('Q')
        replacement = self.tc.transcript.parent / (NATIVE + '.replacement.jsonl')
        replacement.write_bytes(bytes(data))
        return replacement

    def _mutate_one_padding_byte_in_place(self):
        """Rewrite one byte inside an early padding row's own content field, in place (same inode,
        same open path), keeping the file's total size exactly unchanged."""
        data = self.tc.transcript.read_bytes()
        pos = data.index(b'Synthetic padding content') + 5
        new_byte = bytes([ord('Z') if data[pos] != ord('Z') else ord('Q')])
        with open(self.tc.transcript, 'r+b') as fh:
            fh.seek(pos)
            fh.write(new_byte)

    def test_p4_getuid_mismatch_refuses_both_lanes_on_a_giant_transcript(self):
        # F6a: uid ownership was untested at giant scale. inspect() calls validate_source first,
        # which calls ao_native_identity.read_owner on the AO owner database -- itself a
        # getuid-checked file -- before inspect ever reaches its own transcript-ownership check;
        # os.getuid is one process-wide function, so patching it necessarily trips that earlier,
        # unrelated check first, and inspect's own transcript-ownership message is unreachable from
        # this exact provocation (confirmed empirically; see the identical, scale-independent small
        # control in TranscriptIdentityCapacityTests.test_p4_getuid_mismatch_refuses_both_lanes_small_control
        # for the same reason). _read_transcript has no such earlier check and refuses with its own
        # ownership message directly.
        with self.subTest(giant_size=self.giant_size):
            with mock.patch('os.getuid', return_value=os.getuid() + 1):
                with self.assertRaises(RoomError) as ctx:
                    ano.inspect(self.tc.directory, self.state, self.request, self.source, self.snapshot)
                self.assertEqual(str(ctx.exception), 'Cannot establish the retained native outcome owner')
                with self.assertRaises(RoomError) as ctx:
                    et._read_transcript(str(self.tc.transcript), NATIVE)
                self.assertEqual(str(ctx.exception), 'Native transcript ownership, type or size is unsafe')

    def test_p5_inspect_lane_replacement_changes_source_sha256_on_a_giant_transcript(self):
        # F6c: os.replace of a different, valid >64,000,000 byte file over the registered path.
        with self.subTest(giant_size=self.giant_size):
            before = ano.inspect(self.tc.directory, self.state, self.request, self.source, self.snapshot)
            replacement = self._replacement_copy()
            os.replace(str(replacement), str(self.tc.transcript))
            after = ano.inspect(self.tc.directory, self.state, self.request, self.source, self.snapshot)
            self.assertNotEqual(after['source_sha256'], before['source_sha256'])
            self.assertEqual(after['source_sha256'], hashlib.sha256(self.tc.transcript.read_bytes()).hexdigest())
            self.assertEqual(after['errors'], before['errors'])
            self.assertEqual(after['stop_reasons'], before['stop_reasons'])

    def test_p5_transition_lane_replacement_changes_sha256_on_a_giant_transcript(self):
        # F6c: the same os.replace mutation kind, proven in the transition lane.
        with self.subTest(giant_size=self.giant_size):
            before = et._read_transcript(str(self.tc.transcript), NATIVE)
            replacement = self._replacement_copy()
            os.replace(str(replacement), str(self.tc.transcript))
            after = et._read_transcript(str(self.tc.transcript), NATIVE)
            self.assertNotEqual(after['sha256'], before['sha256'])
            self.assertEqual(after['sha256'], hashlib.sha256(self.tc.transcript.read_bytes()).hexdigest())
            self.assertEqual(after['size'], before['size'])
            self.assertEqual(after['records'], before['records'])

    def test_p5_inspect_lane_truncation_removing_the_anchor_fails_correlation_on_a_giant_transcript(self):
        # F6c: a giant-scale counterpart of InspectMutationCapacityTests' truncation_removes_anchor
        # case; same technique (write the stop row before the anchor so truncating right after it
        # drops only the anchor), with giant padding ahead of both rows.
        with open(self.tc.transcript, 'wb') as fh:
            write_padding_bytes(fh, NATIVE, self.workspace, 65_000_000, 0)
            fh.write(_line(self.stop))
            stop_end = fh.tell()
            fh.write(_line(self.anchor))
        giant_size = self.tc.transcript.stat().st_size
        with self.subTest(giant_size=giant_size):
            os.truncate(str(self.tc.transcript), stop_end)
            with self.assertRaises(RoomError) as ctx:
                ano.inspect(self.tc.directory, self.state, self.request, self.source, self.snapshot)
            self.assertEqual(str(ctx.exception), 'Native caller correlation is missing or ambiguous')

    def test_p5_transition_lane_truncation_removing_the_last_launch_result_is_unfinished_on_a_giant_transcript(self):
        # F6c: a giant-scale counterpart of ChildEvidenceMutationCapacityTests'
        # truncation_drops_correlated_result case; this test overrides setUp()'s default
        # anchor/stop shape (unused by this one mutation kind) with giant padding plus one
        # foreground Agent launch/result pair, then drops the result.
        with open(self.tc.transcript, 'wb') as fh:
            write_padding_bytes(fh, NATIVE, self.workspace, 65_000_000, 0)
            use_row, result_row = foreground_completed_rows(self.workspace)
            fh.write(_line(use_row))
            use_end = fh.tell()
            fh.write(_line(result_row))
        giant_size = self.tc.transcript.stat().st_size
        with self.subTest(giant_size=giant_size):
            os.truncate(str(self.tc.transcript), use_end)
            reading = et._read_transcript(str(self.tc.transcript), NATIVE)
            with self.assertRaises(RoomError) as ctx:
                et.child_evidence(reading['events'], NATIVE, self.workspace)
            self.assertEqual(str(ctx.exception), 'A parent Agent/Task launch in the retained transcript has no '
                                                  'correlated result; an unfinished child refuses the transition')

    def test_p5_inspect_lane_same_size_in_place_mutation_changes_source_sha256_on_a_giant_transcript(self):
        # F6c: a same-size, in-place (same inode) content rewrite, distinct from the os.replace
        # (new inode) proof above.
        with self.subTest(giant_size=self.giant_size):
            before = ano.inspect(self.tc.directory, self.state, self.request, self.source, self.snapshot)
            self._mutate_one_padding_byte_in_place()
            self.assertEqual(self.tc.transcript.stat().st_size, self.giant_size)
            after = ano.inspect(self.tc.directory, self.state, self.request, self.source, self.snapshot)
            self.assertNotEqual(after['source_sha256'], before['source_sha256'])
            self.assertEqual(after['source_sha256'], hashlib.sha256(self.tc.transcript.read_bytes()).hexdigest())

    def test_p5_transition_lane_same_size_in_place_mutation_changes_sha256_on_a_giant_transcript(self):
        with self.subTest(giant_size=self.giant_size):
            before = et._read_transcript(str(self.tc.transcript), NATIVE)
            self._mutate_one_padding_byte_in_place()
            after = et._read_transcript(str(self.tc.transcript), NATIVE)
            self.assertEqual(after['size'], before['size'])
            self.assertNotEqual(after['sha256'], before['sha256'])
            self.assertEqual(after['sha256'], hashlib.sha256(self.tc.transcript.read_bytes()).hexdigest())

    def test_p5_inspect_lane_appended_row_changes_source_sha256_on_a_giant_transcript(self):
        with self.subTest(giant_size=self.giant_size, generation_seconds=round(self.generation_seconds, 3)):
            before = ano.inspect(self.tc.directory, self.state, self.request, self.source, self.snapshot)
            pad = padding_pair(NATIVE, self.workspace, 1)
            with open(self.tc.transcript, 'ab') as fh:
                fh.write(pad)
            after = ano.inspect(self.tc.directory, self.state, self.request, self.source, self.snapshot)
            self.assertNotEqual(after['source_sha256'], before['source_sha256'])
            self.assertEqual(after['source_sha256'], hashlib.sha256(self.tc.transcript.read_bytes()).hexdigest())
            self.assertEqual(after['errors'], before['errors'])
            self.assertEqual(after['stop_reasons'], before['stop_reasons'])

    def test_p5_transition_lane_appended_row_changes_sha256_on_a_giant_transcript(self):
        with self.subTest(giant_size=self.giant_size):
            before = et._read_transcript(str(self.tc.transcript), NATIVE)
            pad = padding_pair(NATIVE, self.workspace, 2)
            with open(self.tc.transcript, 'ab') as fh:
                fh.write(pad)
            after = et._read_transcript(str(self.tc.transcript), NATIVE)
            self.assertEqual(set(after), {'sha256', 'size', 'records', 'events', 'offsets', 'ends', 'prefix_sha256'})
            self.assertNotEqual(after['sha256'], before['sha256'])
            self.assertEqual(after['sha256'], hashlib.sha256(self.tc.transcript.read_bytes()).hexdigest())
            self.assertEqual(after['size'], before['size'] + len(pad))
            self.assertEqual(after['records'], before['records'] + 2)


# ---------------------------------------------------------------------------------------------
# P6: malformed JSON, a valid-JSON-non-object row and the per-record byte boundary, each embedded
# inside an otherwise-giant (>64,000,000 byte) history.
# ---------------------------------------------------------------------------------------------

class MalformedRecordCapacityTests(unittest.TestCase):
    """P6: malformed JSON, a valid-JSON non-object row, and the new record-byte-size boundary, each
    embedded inside an otherwise-giant (>64,000,000 byte) history for the inspect lane and the
    _read_transcript lane, so today's whole-file byte cap masks every one of these otherwise-
    unrelated row-level bugs identically -- the same masking story as P1-P3, just for row-level
    checks instead of whole-file admission. The child_evidence-on-raw-bytes lane has no whole-file
    cap of its own (it is handed bytes directly, never a path), so its malformed/non-object checks
    are genuine small-scale controls; only its oversized-record sub-case is inverted (silently
    accepted today regardless of total size, refused once the per-record cap exists).
    """

    @classmethod
    def setUpClass(cls):
        cls.tc = TransitionCase('setUp')
        cls.tc.setUp()
        cls.state = cls.tc.build_room()
        cls.request = cls.state['requests']['spec_review']
        cls.workspace = str(cls.tc.repo)
        cls.source = {'database': str(cls.tc.database), 'transcript': str(cls.tc.transcript),
                      'session_id': SESSION, 'native_session_id': NATIVE}
        cls.snapshot = cls.tc.fake.snapshots[SESSION]
        created_at = cls.request['created_at']
        cls.anchor = {'type': 'user', 'uuid': 'h-anchor', 'sessionId': NATIVE, 'cwd': cls.workspace,
                     'isSidechain': False, 'timestamp': _iso(created_at + 0.001), 'origin': {'kind': 'human'},
                     'message': {'role': 'user', 'content': cls.request['text']}}
        cls.stop = {'type': 'assistant', 'uuid': 'a-reply', 'sessionId': NATIVE, 'cwd': cls.workspace,
                   'isSidechain': False, 'timestamp': _iso(created_at + 0.002),
                   'message': {'role': 'assistant', 'model': FABLE, 'id': 'm0', 'stop_reason': 'end_turn',
                               'content': [{'type': 'text', 'text': 'Synthetic completed answer.'}]}}
        cls.launch_use = {'type': 'assistant', 'uuid': 'launch-1', 'sessionId': NATIVE, 'cwd': cls.workspace,
                          'isSidechain': False, 'message': {'role': 'assistant', 'id': 'm-launch', 'content': [
                              {'type': 'tool_use', 'id': 'toolu_agent1', 'name': 'Agent',
                               'input': {'prompt': 'do work'}}]}}
        cls.launch_result = {'type': 'user', 'uuid': 'result-1', 'sessionId': NATIVE, 'cwd': cls.workspace,
                             'isSidechain': False, 'sourceToolAssistantUUID': 'launch-1',
                             'message': {'role': 'user', 'content': [
                                 {'type': 'tool_result', 'tool_use_id': 'toolu_agent1', 'content': 'Done'}]},
                             'toolUseResult': completed_result('child-agent-1', 'do work')}

    @classmethod
    def tearDownClass(cls):
        cls.tc.doCleanups()

    def _observe(self):
        return ano.inspect(self.tc.directory, self.state, self.request, self.source, self.snapshot)

    def _probe_template(self):
        return {'type': 'assistant', 'uuid': 'pad-big', 'sessionId': NATIVE, 'cwd': self.workspace,
                'isSidechain': False, 'timestamp': PADDING_TIMESTAMP,
                'message': {'role': 'assistant', 'id': 'pad-big-m', 'content': [
                    {'type': 'tool_use', 'id': 'toolu_big', 'name': 'Read', 'input': {'file_path': ''}}]}}

    def _write_giant(self, probe_bytes, target_bytes=65_000_000):
        with open(self.tc.transcript, 'wb') as fh:
            write_padding_bytes(fh, NATIVE, self.workspace, target_bytes, 0)
            fh.write(_line(self.anchor))
            fh.write(_line(self.stop))
            fh.write(probe_bytes)
        return self.tc.transcript.stat().st_size

    def test_p6_inspect_lane_malformed_json_line_refuses_inside_a_giant_history(self):
        size = self._write_giant(b'{not valid json\n')
        with self.subTest(giant_size=size):
            with self.assertRaises(RoomError) as ctx:
                self._observe()
            self.assertEqual(ctx.exception.args[0], getattr(ano, 'TRANSCRIPT_MALFORMED_REFUSAL', None))

    def test_p6_transition_lane_malformed_json_line_refuses_inside_a_giant_history(self):
        size = self._write_giant(b'{not valid json\n')
        with self.subTest(giant_size=size):
            with self.assertRaises(RoomError) as ctx:
                et._read_transcript(str(self.tc.transcript), NATIVE)
            self.assertEqual(str(ctx.exception),
                              'The retained native transcript contains a malformed row; child evidence is unknown')

    def test_p6_inspect_lane_non_object_row_refuses_inside_a_giant_history(self):
        size = self._write_giant(b'42\n')
        with self.subTest(giant_size=size):
            with self.assertRaises(RoomError) as ctx:
                self._observe()
            self.assertEqual(str(ctx.exception), 'Malformed native evidence or changed caller bytes')

    def test_p6_transition_lane_non_object_row_refuses_inside_a_giant_history(self):
        size = self._write_giant(b'42\n')
        with self.subTest(giant_size=size):
            with self.assertRaises(RoomError) as ctx:
                et._read_transcript(str(self.tc.transcript), NATIVE)
            self.assertEqual(str(ctx.exception),
                              'The retained native transcript contains a malformed row; child evidence is unknown')

    def test_p6_inspect_lane_oversized_record_one_byte_over_the_record_budget_refuses_inside_a_giant_history(self):
        line = exact_length_line(self._probe_template(), ano.MAX_TRANSCRIPT_RECORD_BYTES + 1)
        size = self._write_giant(line)
        with self.subTest(giant_size=size):
            with self.assertRaises(RoomError) as ctx:
                self._observe()
            self.assertEqual(ctx.exception.args[0], getattr(ano, 'TRANSCRIPT_RECORD_BYTES_REFUSAL', None))

    def test_p6_transition_lane_oversized_record_one_byte_over_the_record_budget_refuses_inside_a_giant_history(self):
        line = exact_length_line(self._probe_template(), ano.MAX_TRANSCRIPT_RECORD_BYTES + 1)
        size = self._write_giant(line)
        with self.subTest(giant_size=size):
            with self.assertRaises(RoomError) as ctx:
                et._read_transcript(str(self.tc.transcript), NATIVE)
            self.assertEqual(ctx.exception.args[0], getattr(ano, 'TRANSCRIPT_RECORD_BYTES_REFUSAL', None))

    def test_p6_both_lanes_record_at_the_record_budget_boundary_is_admitted_inside_a_giant_history(self):
        line = exact_length_line(self._probe_template(), ano.MAX_TRANSCRIPT_RECORD_BYTES)
        size = self._write_giant(line)
        with self.subTest(lane='inspect', giant_size=size):
            result = self._observe()
            self.assertEqual(result['errors'], [])
            self.assertEqual(result['stop_reasons'], ['end_turn'])
        with self.subTest(lane='transition', giant_size=size):
            reading = et._read_transcript(str(self.tc.transcript), NATIVE)
            self.assertEqual(reading['sha256'], hashlib.sha256(self.tc.transcript.read_bytes()).hexdigest())

    def test_p6_child_evidence_lane_malformed_json_line_refuses(self):
        # control: passes on baseline and after repair (unchanged message; raw bytes carry no
        # whole-file size cap of their own)
        raw = _line(self.launch_use) + b'{not valid json\n' + _line(self.launch_result)
        with self.assertRaises(RoomError) as ctx:
            et.child_evidence(raw, NATIVE, self.workspace)
        self.assertEqual(str(ctx.exception), 'The retained native transcript contains a malformed row; child '
                                              'evidence is unknown')

    def test_p6_child_evidence_lane_non_object_row_refuses(self):
        raw = _line(self.launch_use) + b'42\n' + _line(self.launch_result)  # control: passes on both
        with self.assertRaises(RoomError) as ctx:
            et.child_evidence(raw, NATIVE, self.workspace)
        self.assertEqual(str(ctx.exception), 'The retained native transcript contains a malformed row; child '
                                              'evidence is unknown')

    def test_p6_child_evidence_lane_oversized_record_now_refuses(self):
        # No whole-file cap applies to raw in-memory bytes, so this inverts on its own: today this
        # silently accepts an oversized record regardless of total size; the repair's new per-record
        # cap must refuse it here too, via the same shared constant as both other lanes.
        line = exact_length_line(self._probe_template(), ano.MAX_TRANSCRIPT_RECORD_BYTES + 1)
        raw = _line(self.launch_use) + _line(self.launch_result) + line
        with self.assertRaises(RoomError) as ctx:
            et.child_evidence(raw, NATIVE, self.workspace)
        self.assertEqual(ctx.exception.args[0], getattr(ano, 'TRANSCRIPT_RECORD_BYTES_REFUSAL', None))


# ---------------------------------------------------------------------------------------------
# P9 (new): the future per-file record-count cap (MAX_TRANSCRIPT_RECORDS == 2,000,000).
# ---------------------------------------------------------------------------------------------

class RecordCountBoundaryCapacityTests(unittest.TestCase):
    """P9 (new): exactly 2,000,000 records is admitted; 2,000,001 is refused via
    TRANSCRIPT_RECORD_COUNT_REFUSAL in both lanes. The cap is read as
    getattr(ano, 'MAX_TRANSCRIPT_RECORDS', None) or 2_000_000, so this stays meaningful if that
    constant's value ever moves; it does not exist on this baseline, so the literal 2_000_000 is used
    there. Both fixtures are themselves ~82,000,000 bytes (2,000,000 rows averaging ~41 bytes each),
    so on this baseline the existing 64,000,000 byte whole-file cap also refuses them -- a second,
    independently-sufficient capacity-related reason both are RED today. Uses fast, non-json.dumps
    row construction (record COUNT, not byte shape, must be exact here; benchmarked ~0.45s for
    2,000,000 rows).
    """

    @classmethod
    def setUpClass(cls):
        cls.tc = TransitionCase('setUp')
        cls.tc.setUp()
        cls.state = cls.tc.build_room()
        cls.request = cls.state['requests']['spec_review']
        cls.workspace = str(cls.tc.repo)
        cls.source = {'database': str(cls.tc.database), 'transcript': str(cls.tc.transcript),
                      'session_id': SESSION, 'native_session_id': NATIVE}
        cls.snapshot = cls.tc.fake.snapshots[SESSION]
        created_at = cls.request['created_at']
        cls.anchor = {'type': 'user', 'uuid': 'h-anchor', 'sessionId': NATIVE, 'cwd': cls.workspace,
                     'isSidechain': False, 'timestamp': _iso(created_at + 0.001), 'origin': {'kind': 'human'},
                     'message': {'role': 'user', 'content': cls.request['text']}}
        cls.stop = {'type': 'assistant', 'uuid': 'a-reply', 'sessionId': NATIVE, 'cwd': cls.workspace,
                   'isSidechain': False, 'timestamp': _iso(created_at + 0.002),
                   'message': {'role': 'assistant', 'model': FABLE, 'id': 'm0', 'stop_reason': 'end_turn',
                               'content': [{'type': 'text', 'text': 'Synthetic completed answer.'}]}}
        cls.cap = getattr(ano, 'MAX_TRANSCRIPT_RECORDS', None) or 2_000_000

    @classmethod
    def tearDownClass(cls):
        cls.tc.doCleanups()

    def _observe(self):
        return ano.inspect(self.tc.directory, self.state, self.request, self.source, self.snapshot)

    def test_p9_exactly_the_record_cap_is_admitted_in_both_lanes(self):
        t0 = time.time()
        write_record_count_fixture(self.tc.transcript, self.cap, self.anchor, self.stop)
        generation_seconds = time.time() - t0
        size = self.tc.transcript.stat().st_size
        with self.subTest(lane='inspect', record_count=self.cap, giant_size=size,
                           generation_seconds=round(generation_seconds, 3)):
            result = self._observe()
            self.assertEqual(result['errors'], [])
            self.assertEqual(result['stop_reasons'], ['end_turn'])
        with self.subTest(lane='transition', record_count=self.cap, giant_size=size):
            reading = et._read_transcript(str(self.tc.transcript), NATIVE)
            self.assertEqual(reading['records'], self.cap)

    def test_p9_one_over_the_record_cap_is_refused_in_both_lanes(self):
        t0 = time.time()
        write_record_count_fixture(self.tc.transcript, self.cap + 1, self.anchor, self.stop)
        generation_seconds = time.time() - t0
        size = self.tc.transcript.stat().st_size
        with self.subTest(lane='inspect', record_count=self.cap + 1, giant_size=size,
                           generation_seconds=round(generation_seconds, 3)):
            with self.assertRaises(RoomError) as ctx:
                self._observe()
            self.assertEqual(ctx.exception.args[0], getattr(ano, 'TRANSCRIPT_RECORD_COUNT_REFUSAL', None))
        with self.subTest(lane='transition', record_count=self.cap + 1, giant_size=size):
            with self.assertRaises(RoomError) as ctx:
                et._read_transcript(str(self.tc.transcript), NATIVE)
            self.assertEqual(ctx.exception.args[0], getattr(ano, 'TRANSCRIPT_RECORD_COUNT_REFUSAL', None))


# ---------------------------------------------------------------------------------------------
# P8: a sparse file of exactly 256,000,001 bytes, refused by the size check before any read.
# ---------------------------------------------------------------------------------------------

class SparseTranscriptRefusalTests(unittest.TestCase):
    """P8: a sparse 256,000,001 byte file (one byte over the future 256,000,000 byte cap) is refused
    by every vulnerable reader's size check before any content is read. Unlike P1-P6/P9 (masked on
    this baseline specifically because today's cap is only 64,000,000 bytes), this file is refused by
    today's 64,000,000 byte cap AND would remain refused by the future 256,000,000 byte cap, so this
    boundary anchor is expected to pass on both baseline and after repair (control). Also carries the
    companion boundary one byte lower: a sparse file of exactly 256,000,000 bytes passes the whole-
    file size check (post-repair) but is itself one all-NUL-byte line far past the per-record budget,
    so every reader must instead refuse it via the new record-byte check.
    """

    def test_p8_sparse_256_000_001_byte_file_is_refused_before_any_read(self):
        tc = TransitionCase('setUp')  # control: passes on baseline and after repair
        tc.setUp()
        try:
            state = tc.build_room()
            tc.transcript.write_bytes(b'')
            t0 = time.time()
            os.truncate(str(tc.transcript), 256_000_001)
            elapsed = time.time() - t0
            self.assertEqual(tc.transcript.stat().st_size, 256_000_001)
            source = {'database': str(tc.database), 'transcript': str(tc.transcript),
                      'session_id': SESSION, 'native_session_id': NATIVE}
            request = state['requests']['spec_review']
            snapshot = tc.fake.snapshots[SESSION]
            with self.subTest(reader='ao_native_outcome.preflight_source'):
                with self.assertRaises(RoomError) as ctx:
                    ano.preflight_source(tc.directory, state, str(tc.database), str(tc.transcript))
                self.assertEqual(str(ctx.exception), 'Prospective native transcript file is unsafe')
            with self.subTest(reader='ao_native_outcome.inspect'):
                with self.assertRaises(RoomError) as ctx:
                    ano.inspect(tc.directory, state, request, source, snapshot)
                self.assertEqual(str(ctx.exception), 'Native transcript ownership or size is unsafe')
            with self.subTest(reader='ao_engineering_transition._read_transcript'):
                with self.assertRaises(RoomError) as ctx:
                    et._read_transcript(str(tc.transcript), NATIVE)
                self.assertEqual(str(ctx.exception), 'Native transcript ownership, type or size is unsafe')
            self.assertLess(elapsed, 1.0)
        finally:
            tc.doCleanups()

    def test_p8_sparse_256_000_000_byte_file_passes_the_size_check_but_its_one_line_exceeds_the_record_budget(self):
        tc = TransitionCase('setUp')
        tc.setUp()
        try:
            state = tc.build_room()
            tc.transcript.write_bytes(b'')
            t0 = time.time()
            os.truncate(str(tc.transcript), 256_000_000)
            elapsed = time.time() - t0
            self.assertEqual(tc.transcript.stat().st_size, 256_000_000)
            source = {'database': str(tc.database), 'transcript': str(tc.transcript),
                      'session_id': SESSION, 'native_session_id': NATIVE}
            request = state['requests']['spec_review']
            snapshot = tc.fake.snapshots[SESSION]
            # The whole file is one 256,000,000-byte line of NUL bytes with no trailing newline: not
            # oversized as a *file* at the new cap, but wildly oversized as a single *record* (far
            # past the 16,777,216-byte per-line budget), so every reader must refuse via the new
            # record-byte check, not the (inapplicable) whole-file or malformed-row checks. On this
            # baseline, the old 64,000,000 byte whole-file cap still refuses it first instead.
            with self.subTest(reader='ao_native_outcome.inspect', generation_seconds=round(elapsed, 3)):
                with self.assertRaises(RoomError) as ctx:
                    ano.inspect(tc.directory, state, request, source, snapshot)
                self.assertEqual(ctx.exception.args[0], getattr(ano, 'TRANSCRIPT_RECORD_BYTES_REFUSAL', None))
            with self.subTest(reader='ao_engineering_transition._read_transcript'):
                with self.assertRaises(RoomError) as ctx:
                    et._read_transcript(str(tc.transcript), NATIVE)
                self.assertEqual(ctx.exception.args[0], getattr(ano, 'TRANSCRIPT_RECORD_BYTES_REFUSAL', None))
            self.assertLess(elapsed, 1.0)
        finally:
            tc.doCleanups()


# ---------------------------------------------------------------------------------------------
# F6b (giant scale): the format-recovery lane (ao_spec_verdict_recovery._allowed_tool_uses, fed by
# ao_engineering_transition._read_transcript's own prefix_bytes/prefix_sha256 support) had no
# real file >64,000,000 byte proof, and no proof at all against a transcript whose own records are
# a genuine mix of '\n' and '\r\n' terminators.
# ---------------------------------------------------------------------------------------------

def _mixed_terminator_line(row, crlf):
    """Exactly like _line, but the caller picks '\\n' (crlf=False) or '\\r\\n' (crlf=True), so a
    file built from a sequence of these calls is a genuine mix of both, not just one throughout."""
    body = json.dumps(row, sort_keys=True, ensure_ascii=False).encode('utf-8')
    return body + (b'\r\n' if crlf else b'\n')


def write_mixed_terminator_padding(fh, approx_bytes, start_index):
    """Bulk filler for the F6b giant fixture: inert {'type': 'progress'} rows, which
    _allowed_tool_uses always 'continue's past (wrong type for its own tool-use inspection)
    regardless of which side of a tested boundary they land on, carrying the module's existing
    ~50,000-byte PAD_CONTENT block for speed. Terminators alternate '\\n'/'\\r\\n' by index parity,
    so the mix is genuine throughout the file, not only at its hand-placed boundary/tool rows.
    Distinct from write_padding_bytes/padding_pair (unchanged, reused elsewhere): those are
    single-terminator and carry a Read tool_use block, which this fixture's suffix regions must
    never contain.
    """
    if approx_bytes <= 0:
        return start_index, 0
    index = start_index
    written = 0
    batch, batch_bytes = [], 0
    while written < approx_bytes:
        index += 1
        row = {'type': 'progress', 'uuid': 'f6b-pad-%010d' % index, 'pad': PAD_CONTENT}
        block = _mixed_terminator_line(row, index % 2 == 0)
        batch.append(block)
        batch_bytes += len(block)
        written += len(block)
        if batch_bytes >= 8_000_000:
            fh.write(b''.join(batch))
            batch, batch_bytes = [], 0
    if batch:
        fh.write(b''.join(batch))
    return index, written


def _allowed_tool_row(uuid_value, tool_id, name):
    return {'type': 'assistant', 'uuid': uuid_value,
            'message': {'role': 'assistant', 'content': [
                {'type': 'tool_use', 'id': tool_id, 'name': name, 'input': {}}]}}


class RecoveryGiantTranscriptCapacityTests(unittest.TestCase):
    """F6b: one >64,000,000 byte, mixed '\\n'/'\\r\\n'-terminated fixture, built once in setUpClass
    and never mutated afterwards (read-only for every test method below, so only one large file is
    ever alive from this class), proves ao_engineering_transition._read_transcript's prefix_bytes/
    prefix_sha256 support and ao_spec_verdict_recovery._allowed_tool_uses together at three genuinely
    different EARLY/MIDDLE/LATE prefix boundaries (each at its exact record end before the
    terminator and its exact start right after it), plus a mid-record straddle refusal, a
    disallowed-tool refusal and an over-size prefix. A single disallowed (Bash) tool_use row sits
    before all three boundaries precisely so it never contaminates their own happy-path suffix
    regions; its own dedicated, earlier boundary is what proves the disallowed-tool refusal. Every
    row below carries its own unique uuid; no row after any tested boundary is ever a sidechain or
    agent row (both are proven separately and need neither here). ALLOWED_TOOLS rows accumulate
    forward (allowed-1 before EARLY, allowed-2 before MIDDLE, allowed-3 and allowed-4 before/after
    LATE), so EARLY/MIDDLE/LATE each expect a different, boundary-sensitive subset -- a stronger
    proof than three copies of the same inclusion check at different hash points. Builds its own
    TransitionCase purely for its already-proven safe-owned tmp-file environment; the two functions
    under test need no spec/request/room state at all.
    """

    @classmethod
    def setUpClass(cls):
        cls.tc = TransitionCase('setUp')
        cls.tc.setUp()
        cls.tc.build_room()  # the only route to a registered self.transcript path (and its owner)
        cls.path = cls.tc.transcript  # name is load-bearing: must be exactly NATIVE + '.jsonl'
        boundaries = {}
        with open(cls.path, 'wb') as fh:
            before_bash = {'type': 'progress', 'uuid': 'f6b-marker-before-bash'}
            fh.write(_mixed_terminator_line(before_bash, False))
            boundaries['disallowed'] = fh.tell()
            fh.write(_mixed_terminator_line(
                _allowed_tool_row('f6b-bash-row', 'toolu_f6b_bash_disallowed', 'Bash'), False))

            write_mixed_terminator_padding(fh, 22_000_000, 0)
            cls.early_start = fh.tell()
            fh.write(_mixed_terminator_line({'type': 'progress', 'uuid': 'f6b-marker-early'}, True))
            boundaries['early_end'] = fh.tell() - 2
            boundaries['early_after'] = fh.tell()
            fh.write(_mixed_terminator_line(
                _allowed_tool_row('f6b-allowed-1', 'toolu_f6b_allowed_1', 'TaskCreate'), False))

            write_mixed_terminator_padding(fh, 22_000_000, 1_000_000)
            fh.write(_mixed_terminator_line({'type': 'progress', 'uuid': 'f6b-marker-middle'}, True))
            boundaries['middle_end'] = fh.tell() - 2
            boundaries['middle_after'] = fh.tell()
            fh.write(_mixed_terminator_line(
                _allowed_tool_row('f6b-allowed-2', 'toolu_f6b_allowed_2', 'TaskGet'), False))

            write_mixed_terminator_padding(fh, 22_000_000, 2_000_000)
            fh.write(_mixed_terminator_line({'type': 'progress', 'uuid': 'f6b-marker-late'}, True))
            boundaries['late_end'] = fh.tell() - 2
            boundaries['late_after'] = fh.tell()
            fh.write(_mixed_terminator_line(
                _allowed_tool_row('f6b-allowed-3', 'toolu_f6b_allowed_3', 'TaskList'), False))
            fh.write(_mixed_terminator_line(
                _allowed_tool_row('f6b-allowed-4', 'toolu_f6b_allowed_4', 'TaskUpdate'), True))
        cls.boundaries = boundaries
        cls.size = cls.path.stat().st_size
        assert cls.size > 64_000_000, cls.size  # fixture precondition, not the behaviour under test

    @classmethod
    def tearDownClass(cls):
        cls.tc.doCleanups()

    def _read(self, prefix_bytes):
        return et._read_transcript(str(self.path), NATIVE, prefix_bytes=prefix_bytes)

    def _assert_prefix(self, n):
        reading = self._read(n)
        self.assertEqual(reading['size'], self.size)
        self.assertEqual(reading['prefix_sha256'], hashlib.sha256(self.path.read_bytes()[:n]).hexdigest())
        return reading

    def test_f6b_early_boundary_before_terminator_sees_every_allowed_tool_use(self):
        n = self.boundaries['early_end']
        with self.subTest(size=self.size, n=n):
            reading = self._assert_prefix(n)
            allowed = recovery._allowed_tool_uses(reading, {'bytes': n})
            self.assertEqual(allowed, {'toolu_f6b_allowed_1', 'toolu_f6b_allowed_2',
                                        'toolu_f6b_allowed_3', 'toolu_f6b_allowed_4'})

    def test_f6b_early_boundary_after_terminator_sees_every_allowed_tool_use(self):
        n = self.boundaries['early_after']
        with self.subTest(size=self.size, n=n):
            reading = self._assert_prefix(n)
            allowed = recovery._allowed_tool_uses(reading, {'bytes': n})
            self.assertEqual(allowed, {'toolu_f6b_allowed_1', 'toolu_f6b_allowed_2',
                                        'toolu_f6b_allowed_3', 'toolu_f6b_allowed_4'})

    def test_f6b_middle_boundary_before_terminator_excludes_the_earlier_allowed_tool_use(self):
        n = self.boundaries['middle_end']
        with self.subTest(size=self.size, n=n):
            reading = self._assert_prefix(n)
            allowed = recovery._allowed_tool_uses(reading, {'bytes': n})
            self.assertEqual(allowed, {'toolu_f6b_allowed_2', 'toolu_f6b_allowed_3', 'toolu_f6b_allowed_4'})

    def test_f6b_middle_boundary_after_terminator_excludes_the_earlier_allowed_tool_use(self):
        n = self.boundaries['middle_after']
        with self.subTest(size=self.size, n=n):
            reading = self._assert_prefix(n)
            allowed = recovery._allowed_tool_uses(reading, {'bytes': n})
            self.assertEqual(allowed, {'toolu_f6b_allowed_2', 'toolu_f6b_allowed_3', 'toolu_f6b_allowed_4'})

    def test_f6b_late_boundary_before_terminator_sees_only_the_latest_allowed_tool_uses(self):
        n = self.boundaries['late_end']
        with self.subTest(size=self.size, n=n):
            reading = self._assert_prefix(n)
            allowed = recovery._allowed_tool_uses(reading, {'bytes': n})
            self.assertEqual(allowed, {'toolu_f6b_allowed_3', 'toolu_f6b_allowed_4'})

    def test_f6b_late_boundary_after_terminator_sees_only_the_latest_allowed_tool_uses(self):
        n = self.boundaries['late_after']
        with self.subTest(size=self.size, n=n):
            reading = self._assert_prefix(n)
            allowed = recovery._allowed_tool_uses(reading, {'bytes': n})
            self.assertEqual(allowed, {'toolu_f6b_allowed_3', 'toolu_f6b_allowed_4'})

    def test_f6b_boundary_inside_a_record_refuses(self):
        n = self.early_start + 1
        with self.subTest(size=self.size, n=n):
            reading = self._read(n)
            with self.assertRaises(RoomError) as ctx:
                recovery._allowed_tool_uses(reading, {'bytes': n})
            self.assertEqual(str(ctx.exception), recovery.TOOL_REFUSAL)

    def test_f6b_disallowed_tool_after_the_boundary_refuses(self):
        n = self.boundaries['disallowed']
        with self.subTest(size=self.size, n=n):
            reading = self._read(n)
            with self.assertRaises(RoomError) as ctx:
                recovery._allowed_tool_uses(reading, {'bytes': n})
            self.assertEqual(str(ctx.exception), recovery.TOOL_REFUSAL)

    def test_f6b_boundary_past_end_of_file_has_no_prefix_digest(self):
        n = self.size + 1000
        with self.subTest(size=self.size, n=n):
            reading = self._read(n)
            self.assertIsNone(reading['prefix_sha256'])
            self.assertEqual(reading['size'], self.size)


# ---------------------------------------------------------------------------------------------
# P10 (operator-run only): the exact production cap boundary at 256,000,000 bytes, with wall-time
# and peak-RSS evidence. Skipped by default: this probe's memory/time profile is deliberately not
# bounded by this module's otherwise-sub-second budget, and is only meaningful once the repair (and
# its chosen streaming/bounded-memory strategy, if any) actually exists.
# ---------------------------------------------------------------------------------------------

class CapacityProbeTests(unittest.TestCase):
    """P10 (operator-run only): builds one exact 256,000,000 byte file and confirms it is admitted,
    then appends one byte and confirms every vulnerable reader now refuses it, recording wall time and
    peak RSS for each call. Set PROJECT_ROOM_CAPACITY_PROBE=1 to run; skipped otherwise.
    """

    @unittest.skipUnless(os.environ.get('PROJECT_ROOM_CAPACITY_PROBE') == '1',
                          'operator-run only; set PROJECT_ROOM_CAPACITY_PROBE=1 to execute')
    def test_p10_production_cap_256_000_000_bytes_operator_probe(self):
        import platform
        import resource
        tc = TransitionCase('setUp')
        tc.setUp()
        try:
            state = tc.build_room()
            request = state['requests']['spec_review']
            workspace = str(tc.repo)
            source = {'database': str(tc.database), 'transcript': str(tc.transcript),
                      'session_id': SESSION, 'native_session_id': NATIVE}
            snapshot = tc.fake.snapshots[SESSION]
            created_at = request['created_at']
            anchor = {'type': 'user', 'uuid': 'h-anchor', 'sessionId': NATIVE, 'cwd': workspace,
                      'isSidechain': False, 'timestamp': _iso(created_at + 0.001), 'origin': {'kind': 'human'},
                      'message': {'role': 'user', 'content': request['text']}}
            stop = {'type': 'assistant', 'uuid': 'a-reply', 'sessionId': NATIVE, 'cwd': workspace,
                    'isSidechain': False, 'timestamp': _iso(created_at + 0.002),
                    'message': {'role': 'assistant', 'model': FABLE, 'id': 'm0', 'stop_reason': 'end_turn',
                                'content': [{'type': 'text', 'text': 'Synthetic completed answer.'}]}}
            head = _line(anchor) + _line(stop)
            # Operator-run only, and only ever invoked once the repair has actually landed (see this
            # module's run instructions), so the three budget constants are read directly off the
            # production module -- no getattr fallback -- and this probe only ever measures the real
            # repaired contract, never a hardcoded guess that could silently drift from it.
            cap = ano.MAX_TRANSCRIPT_BYTES
            remaining = cap - len(head)
            pair = padding_pair(NATIVE, workspace, 0)
            whole_pairs, remainder = divmod(remaining, len(pair))
            filler = None
            if remainder:
                # exact_length_line's returned line always carries one trailing newline byte beyond
                # its requested target_bytes content length, so the filler that must land in exactly
                # `remainder` bytes has to be asked for target_bytes = remainder - 1 (F5: asking for
                # exactly `remainder` here landed the whole file one byte over cap on every non-zero
                # remainder, and raised ValueError outright for the smallest remainders). When
                # `remainder - 1` is below this row's own minimum unpadded size, borrow one whole
                # repeated pair back out of whole_pairs and let the filler absorb it instead, so
                # there is always enough room to shrink to any remainder.
                filler_row = {'type': 'progress', 'uuid': 'p-exact-cap', 'pad': ''}
                target = remainder - 1
                try:
                    filler = exact_length_line(filler_row, target, pad_field=('pad',))
                except ValueError:
                    assert whole_pairs >= 1, 'not enough whole pairs to borrow one back for the filler'
                    whole_pairs -= 1
                    target += len(pair)
                    filler = exact_length_line(filler_row, target, pad_field=('pad',))
            with open(tc.transcript, 'wb') as fh:
                fh.write(head)
                chunk = 200
                written_pairs = 0
                while written_pairs < whole_pairs:
                    take = min(chunk, whole_pairs - written_pairs)
                    fh.write(pair * take)
                    written_pairs += take
                if filler is not None:
                    fh.write(filler)
            size = tc.transcript.stat().st_size
            self.assertEqual(size, ano.MAX_TRANSCRIPT_BYTES)

            def rss_bytes():
                raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                return raw if sys.platform == 'darwin' else raw * 1024

            timings = {}

            def timed(label, call):
                t0 = time.time()
                value = call()
                timings[label] = time.time() - t0
                return value

            inspect_result = timed('inspect', lambda: ano.inspect(tc.directory, state, request, source, snapshot))

            def read_and_evidence():
                reading = et._read_transcript(str(tc.transcript), NATIVE)
                events = reading['events'] if isinstance(reading, dict) else reading
                et.child_evidence(events, NATIVE, workspace)
                return reading

            reading = timed('read_transcript_and_child_evidence', read_and_evidence)
            timed('preflight_source', lambda: ano.preflight_source(tc.directory, state, str(tc.database),
                                                                    str(tc.transcript)))
            timed('validate_registered_source', lambda: ano.validate_registered_source(tc.directory, state))
            peak_rss = rss_bytes()
            self.assertEqual(inspect_result['source_sha256'], hashlib.sha256(tc.transcript.read_bytes()).hexdigest())

            with open(tc.transcript, 'ab') as fh:
                fh.write(b'\n')
            over_size = tc.transcript.stat().st_size
            self.assertEqual(over_size, cap + 1)
            over_cap_refusals = {}
            for label, call in (
                ('inspect', lambda: ano.inspect(tc.directory, state, request, source, snapshot)),
                ('_read_transcript', lambda: et._read_transcript(str(tc.transcript), NATIVE)),
                ('preflight_source', lambda: ano.preflight_source(tc.directory, state, str(tc.database),
                                                                    str(tc.transcript))),
                ('validate_registered_source', lambda: ano.validate_registered_source(tc.directory, state)),
            ):
                with self.subTest(over_cap_reader=label):
                    with self.assertRaises(RoomError) as ctx:
                        call()
                    over_cap_refusals[label] = str(ctx.exception)

            payload = {
                'size': size, 'over_bytes': over_size, 'record_count': reading['records'],
                'python_version': platform.python_version(), 'sys_platform': sys.platform,
                'machine': platform.machine(),
                'max_transcript_bytes': ano.MAX_TRANSCRIPT_BYTES,
                'max_transcript_record_bytes': ano.MAX_TRANSCRIPT_RECORD_BYTES,
                'max_transcript_records': ano.MAX_TRANSCRIPT_RECORDS,
                'wall_seconds': {key: round(value, 3) for key, value in timings.items()},
                'peak_rss_bytes': peak_rss, 'over_cap_refusals': over_cap_refusals,
            }
            print('CAPACITY_PROBE ' + json.dumps(payload))
            output_path = os.environ.get('PROJECT_ROOM_CAPACITY_PROBE_OUTPUT')
            if output_path:
                Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                Path(output_path).write_text(json.dumps(payload) + '\n')
        finally:
            tc.doCleanups()


if __name__ == '__main__':
    unittest.main()
