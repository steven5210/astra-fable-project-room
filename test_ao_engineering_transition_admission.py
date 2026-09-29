'''Qualified admission integration for the audited engineering-model transition core; no AO or model calls.

These tests compose the supplied synthetic normal-room/preparation/native-owner/transcript fixture with the
real ao_engineering_model policy and epoch readers, the real ao_model_qualification retention and lower
admission helper, the real create-once journal and the real room lock. Only the AO transport, the pinned
routing-file revalidation and the recorded executable identity boundary are injected, exactly as the core's own
suite does; no test replaces the admission helper, the target selection or the retention writer under test.
Nothing here claims to have been executed.
'''

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch

import ao_engineering_model as em
import ao_engineering_transition as et
import ao_model_qualification as qmod
import ao_project_room as ao
import ao_routing
from room import RoomError
from test_ao_engineering_transition import (FABLE, NATIVE, OPUS, ROOM, SESSION, RULES, TransitionCase,
                                           files_under)

CLI_VERSION = '2.1.282 (Claude Code)'
FLOOR = '2.1.280'
WORKER_SONNET = 'claude-sonnet-5-5'
NEW_FABLE = 'claude-fable-5-2'
NEW_OPUS = 'claude-opus-5-6'


class QualifiedTransitionCase(TransitionCase):
    '''One synthetic normal room whose pinned version 3 policy carries a private family qualification.'''

    def executable_identity(self, directory, state, prepared):
        return copy.deepcopy(self.recorded)

    def stubs(self):
        return [patch.object(ao_routing, 'validate_local', self.validate_local_stub),
                patch.object(ao_routing, 'observe_rules', lambda client, state: copy.deepcopy(RULES)),
                patch.object(ao_routing, 'rules_match', lambda observed, recorded: observed == recorded),
                patch.object(em, 'executable_identity', self.executable_identity)]

    def setUp(self):
        super().setUp()
        keys = sorted(set(ao_routing.RECORDED_ENV) | set(ao_routing.ALIAS_ENV) | set(ao_routing.GATEWAY_ENV)
                      | set(ao_routing.FOREGROUND_ENV) | set(ao_routing.CONTRADICTORY_ENV))
        saved = {key: os.environ.pop(key) for key in keys if key in os.environ}
        self.addCleanup(lambda: os.environ.update(saved))
        self.cli = self.root / 'fake-claude-version'
        self.cli.write_text('#!' + sys.executable + '\nprint("' + CLI_VERSION + '")\n')
        self.cli.chmod(0o700)
        self.recorded = ao_routing.claude_evidence(str(self.cli))
        self.assertEqual(self.recorded['version'], CLI_VERSION)
        self.project_env = {}
        original = self.fake.request

        def request(method, path, payload=None):
            base = path.split('?')[0]
            if method == 'GET' and base.startswith('/projects/'):
                return {'project': {'id': base.split('/')[-1], 'config': {'env': dict(self.project_env)}}}
            return original(method, path, payload)

        self.fake.request = request

    # --- synthetic qualification fixture --------------------------------------------------
    def evidence_file(self, revision):
        path = self.root / 'evidence' / ('opus-family-%d.txt' % revision)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('Synthetic operator-retained documentation excerpt %d.\n' % revision)
        path.chmod(0o600)
        return path

    def artifact(self, families=None, revision=3):
        path = self.evidence_file(revision)
        descriptor = {'id': 'opus-family-doc', 'uri': 'https://docs.invalid.example/claude/opus-family',
                      'captured_at': '2026-06-01T00:00:00Z',
                      'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'evidence_file': str(path)}
        value = {'format': qmod.FORMAT, 'revision': revision, 'qualified_at': '2026-06-02T00:00:00Z',
                 'scope': copy.deepcopy(qmod.SCOPE),
                 'families': {'opus': {'expected_model': OPUS, 'source_ids': ['opus-family-doc'],
                                       'minimum_claude_code_version': FLOOR}},
                 'sources': [descriptor]}
        if families is not None:
            value['families'] = families
        return value

    def private_evidence(self, artifact):
        return Path(artifact['sources'][0]['evidence_file'])

    def configure(self, artifact):
        path = self.root / ('qualification-' + qmod.digest(artifact)[:16] + '.json')
        path.write_text(json.dumps(artifact, indent=2, sort_keys=True))
        path.chmod(0o600)
        config = self.home / 'ao' / 'config.json'
        value = ao.read(config) if config.exists() else {}
        value['family_qualification'] = {'path': str(path), 'sha256': qmod.digest(artifact)}
        ao.atomic(config, value)
        return path

    def unconfigure(self):
        config = self.home / 'ao' / 'config.json'
        value = ao.read(config) if config.exists() else {}
        value.pop('family_qualification', None)
        ao.atomic(config, value)

    def qualified_room(self, model='fable', artifact=None):
        artifact = self.artifact() if artifact is None else artifact
        self.artifact_value = artifact
        self.artifact_path = self.configure(artifact)
        rooms = self.home / 'ao' / 'rooms'
        if rooms.exists():
            shutil.rmtree(rooms)
        ledger = self.home / 'deepseek'
        if ledger.exists():
            shutil.rmtree(ledger)
        self.fake.reset()
        state = self.build_room(model=model, selection=True)
        qmod.retain(self.directory, state[em.SELECTION_KEY]['family_qualification'])
        return state

    def use_family_workers(self):
        prepared = ao.read(self.directory / 'preparation.json')
        routing = {**prepared['routing'], 'version': 2, 'agents': dict(ao_routing.FAMILY_AGENTS),
                   'agent_selection': {name: dict(item) for name, item in ao_routing.AGENT_SELECTION.items()},
                   'agent_identity_basis': ao_routing.AGENT_IDENTITY_BASIS,
                   'claude': copy.deepcopy(self.recorded)}
        routing.pop('worker_qualification', None)
        prepared['routing'] = routing
        ao.atomic(self.directory / 'preparation.json', prepared)
        state = self.state()
        state['preparation_sha256'] = ao.digest(prepared)
        ao.atomic(self.directory / 'state.json', state)
        return routing

    def snapshot_of(self, artifact):
        return qmod.snapshot({'artifact': artifact, 'sha256': qmod.digest(artifact)})

    def use_qualified_workers(self, artifact):
        snapshot = self.snapshot_of(artifact)
        value = ao_routing.worker_qualification_value(snapshot)
        qmod.retain(self.directory, snapshot)
        prepared = ao.read(self.directory / 'preparation.json')
        routing = {**prepared['routing'], 'version': 3, 'agents': ao_routing.qualified_worker_map(value),
                   'agent_selection': {name: dict(item) for name, item in ao_routing.AGENT_SELECTION.items()},
                   'agent_identity_basis': ao_routing.QUALIFIED_IDENTITY_BASIS, 'worker_qualification': value,
                   'claude': copy.deepcopy(self.recorded)}
        prepared['routing'] = routing
        ao.atomic(self.directory / 'preparation.json', prepared)
        state = self.state()
        state['preparation_sha256'] = ao.digest(prepared)
        ao.atomic(self.directory / 'state.json', state)
        return value

    def retained_capture(self, artifact):
        return self.directory / self.snapshot_of(artifact)['evidence'][0]['record']

    def fable_artifact(self, expected=FABLE, revision=3):
        return self.artifact(families={'fable': {'expected_model': expected, 'source_ids': ['opus-family-doc']}},
                             revision=revision)


class QualifiedAdmissionTests(QualifiedTransitionCase):
    def test_audit_and_transition_admit_retain_commit_and_freeze(self):
        self.qualified_room()
        self.use_family_workers()
        before = files_under(self.directory)
        result = self.audit('opus')
        self.assertTrue(result['eligible'], result.get('reason'))
        self.assertEqual(result['target']['selector'], {'kind': 'family', 'family': 'opus'})
        self.assertEqual(result['expected_model'], OPUS)
        self.assertEqual(result['qualification_sha256'], qmod.digest(self.artifact_value))
        self.assertIsNone(result['observed_served_model'])
        self.assertEqual(files_under(self.directory), before)
        self.assertEqual(self.fake.patches, 0)
        result, arguments = self.request_arguments(target='opus', request_id='model-opus')
        outcome = self.run_transition(arguments)
        self.assertTrue(outcome['transitioned'], outcome)
        self.assertEqual(self.fake.patches, 1)
        self.assertEqual(outcome['expected_model'], OPUS)
        self.assertEqual(outcome['qualification_sha256'], qmod.digest(self.artifact_value))
        self.assertEqual(outcome['expected_model_basis'], 'operator-selected family qualification')
        self.assertIsNone(outcome['observed_served_model'])
        self.assertEqual(outcome['effective_effort']['effective'], 'not evidenced')
        state = self.state()
        epoch = em.current(self.directory, state)
        self.assertTrue(epoch['family_qualified'])
        self.assertEqual(epoch['expected_model'], OPUS)
        self.assertEqual(epoch['qualification_sha256'], qmod.digest(self.artifact_value))
        context = em.qualification_context(self.directory, state, 'model-opus')
        self.assertEqual(context['source'], 'committed_target')
        pinned = qmod.verify_reference(self.directory, context['reference'], 'synthetic retained proof')
        self.assertEqual(pinned['sha256'], qmod.digest(self.artifact_value))
        self.assertTrue(pinned['retained_evidence'])
        # The next frozen request carries the pre-inference expectation, and nothing was inferred: the
        # supported registration names this room's own owner database and transcript, and the real
        # freeze guard validates that exact identity before a request could be created.
        from ao_native_outcome import preflight_source
        state = self.state()
        registered = preflight_source(self.directory, state, str(self.database), str(self.transcript))
        self.assertEqual(registered['source'],
                         {'database': str(self.database), 'transcript': str(self.transcript),
                          'session_id': SESSION, 'native_session_id': NATIVE})
        self.assertEqual(registered['registered'], 'created')
        ao.atomic(self.directory / 'state.json', state)
        frozen = em.freeze_request(self.directory, state)
        self.assertEqual(frozen['version'], 2)
        self.assertEqual(frozen['selector'], {'kind': 'family', 'family': 'opus'})
        self.assertEqual(frozen['configured_model'], 'opus')
        self.assertEqual(frozen['expected_model'], OPUS)
        self.assertEqual(frozen['qualification_sha256'], qmod.digest(self.artifact_value))
        self.assertIsNone(frozen['resolution_sha256'])

    def test_same_family_new_qualification_opens_a_new_epoch_without_a_patch(self):
        first = self.fable_artifact()
        self.qualified_room(model='fable', artifact=first)
        self.use_family_workers()
        second = self.fable_artifact(expected=NEW_FABLE, revision=4)
        self.configure(second)
        self.artifact_value = second
        result = self.audit('fable')
        self.assertTrue(result['eligible'], result.get('reason'))
        self.assertTrue(result['reset'])
        self.assertIsNone(result['patch'])
        self.assertEqual(result['expected_model'], NEW_FABLE)
        self.assertEqual(result['qualification_sha256'], qmod.digest(second))
        result, arguments = self.request_arguments(target='fable', request_id='model-reset')
        outcome = self.run_transition(arguments)
        self.assertTrue(outcome['transitioned'], outcome)
        self.assertEqual(self.fake.patches, 0)
        self.assertIsNone(outcome['attempt_sha256'])
        self.assertEqual(outcome['expected_model'], NEW_FABLE)
        self.assertEqual(outcome['qualification_sha256'], qmod.digest(second))
        state = self.state()
        epochs = em.epochs(self.directory, state)
        self.assertEqual([item['configured_model'] for item in epochs], ['fable', 'fable'])
        self.assertEqual(epochs[0]['qualification_sha256'], qmod.digest(first))
        self.assertEqual(epochs[-1]['qualification_sha256'], qmod.digest(second))
        self.assertTrue(epochs[-1]['family_qualified'])
        self.assertEqual(epochs[-1]['expected_model'], NEW_FABLE)
        self.assertFalse((self.directory / (em.BASE + '/attempts/model-reset.json')).exists())
        pinned = qmod.verify_reference(self.directory,
                                       {key: self.snapshot_of(second)[key]
                                        for key in ('sha256', 'record', 'evidence')},
                                       'synthetic retained proof')
        self.assertTrue(pinned['retained_evidence'])

    def test_missing_family_qualification_refuses_before_any_mutation(self):
        self.qualified_room()
        self.use_family_workers()
        result = self.audit('fable')
        self.assertFalse(result['eligible'])
        self.assertIn('operator-selected family qualification', result['reason'])
        arguments = {'room_id': ROOM, 'request_id': 'model-missing', 'source_model': FABLE,
                     'target_model': 'fable', 'audit_sha256': 'a' * 64, 'spec_record_sha256': 'b' * 64,
                     'candidate_sha256': 'c' * 64, 'native_history_sha256': 'd' * 64,
                     'native_owner_database': str(self.database),
                     'native_transcript_path': str(self.transcript),
                     'authorization': 'The user authorized this exact engineering model transition',
                     'reason': 'A synthetic attempt without the operator-selected qualification'}
        with self.assertRaisesRegex(RoomError, 'operator-selected family qualification'):
            et.transition(self.service, **arguments)
        self.assertIsNone(self.state().get(em.POINTER_KEY))
        self.assertFalse((self.directory / em.BASE).exists())
        self.assertEqual(self.fake.patches, 0)
        # With no configured qualification at all the same family target is refused identically.
        self.unconfigure()
        result = self.audit('opus')
        self.assertFalse(result['eligible'])
        self.assertIn('operator-selected family qualification', result['reason'])

    def test_target_current_and_worker_qualifications_stay_independent(self):
        current = self.fable_artifact()
        self.qualified_room(model='fable', artifact=current)
        worker = self.artifact(revision=9, families={
            'opus': {'expected_model': OPUS, 'source_ids': ['opus-family-doc'],
                     'minimum_claude_code_version': FLOOR},
            'sonnet': {'expected_model': WORKER_SONNET, 'source_ids': ['opus-family-doc']}})
        self.use_qualified_workers(worker)
        target = self.artifact(revision=11)
        self.configure(target)
        self.artifact_value = target
        self.assertNotEqual(qmod.digest(current), qmod.digest(target))
        self.assertNotEqual(qmod.digest(target), qmod.digest(worker))
        self.assertNotEqual(qmod.digest(current), qmod.digest(worker))
        result, arguments = self.request_arguments(target='opus', request_id='model-distinct')
        admitted = result['evidence']['qualification']
        self.assertEqual(admitted['sha256'], qmod.digest(target))
        self.assertEqual(admitted['proof'], 'private_prospective')
        self.assertEqual(admitted['worker_reference']['sha256'], qmod.digest(worker))
        self.assertEqual(admitted['worker_expected_models']['pr-opus'], OPUS)
        outcome = self.run_transition(arguments)
        self.assertTrue(outcome['transitioned'], outcome)
        self.assertEqual(self.fake.patches, 1)
        state = self.state()
        epochs = em.epochs(self.directory, state)
        self.assertEqual(epochs[0]['qualification_sha256'], qmod.digest(current))
        self.assertEqual(epochs[-1]['qualification_sha256'], qmod.digest(target))
        self.assertTrue(epochs[-1]['family_qualified'])
        self.assertEqual(epochs[-1]['expected_model'], OPUS)
        for artifact in (current, worker, target):
            pinned = qmod.verify_reference(self.directory,
                                           {key: self.snapshot_of(artifact)[key]
                                            for key in ('sha256', 'record', 'evidence')},
                                           'synthetic retained proof')
            self.assertTrue(pinned['retained_evidence'])
        record = ao.read(self.directory / (em.BASE + '/records/model-distinct.json'))
        self.assertEqual(record['target']['policy_sha256'], ao.digest(record['target']['policy']))
        context = em.qualification_context_for_target(record['target'])
        self.assertEqual(context['expected_model'], OPUS)
        self.assertEqual(context['reference']['sha256'], qmod.digest(target))

    def test_partial_identical_retry_uses_the_recorded_target_and_retained_proof(self):
        self.qualified_room()
        self.use_family_workers()
        result, arguments = self.request_arguments(target='opus', request_id='model-replay')
        self.fake.patch = 'refuse'
        pending = self.run_transition(arguments)
        self.assertTrue(pending['pending'])
        self.assertEqual(self.fake.patches, 1)
        # Every private original disappears and the active selection moves to a different artifact.
        self.artifact_path.unlink()
        self.private_evidence(self.artifact_value).unlink()
        replacement = self.artifact(revision=7, families={
            'opus': {'expected_model': NEW_OPUS, 'source_ids': ['opus-family-doc'],
                     'minimum_claude_code_version': FLOOR}})
        self.configure(replacement)
        self.fake.snapshots[SESSION]['settings'] = {'model': 'opus', 'reasoningEffort': 'max'}
        outcome = self.run_transition(arguments)
        self.assertTrue(outcome['transitioned'], outcome)
        self.assertEqual(self.fake.patches, 1)
        self.assertEqual(outcome['expected_model'], OPUS)
        self.assertEqual(outcome['qualification_sha256'], qmod.digest(self.artifact_value))
        state = self.state()
        self.assertEqual(em.current(self.directory, state)['expected_model'], OPUS)
        pinned = qmod.verify_reference(
            self.directory, {key: self.snapshot_of(self.artifact_value)[key]
                             for key in ('sha256', 'record', 'evidence')}, 'synthetic retained proof')
        self.assertTrue(pinned['retained_evidence'])
        # A fresh audit would now select the changed active artifact; the replay never consulted it.
        fresh = self.audit('opus')
        self.assertTrue(fresh['eligible'], fresh.get('reason'))
        self.assertEqual(fresh['expected_model'], NEW_OPUS)
        self.assertNotEqual(fresh['audit_sha256'], result['audit_sha256'])

    def test_damaged_retained_target_source_refuses_without_repair(self):
        for case in ('missing', 'changed'):
            with self.subTest(case=case):
                current = self.fable_artifact()
                self.qualified_room(model='fable', artifact=current)
                self.use_family_workers()
                target = self.artifact(revision=11)
                self.configure(target)
                self.artifact_value = target
                self.assertNotEqual(qmod.digest(current), qmod.digest(target))
                self.assert_retained_proof(current, 'initial room authority remains valid')
                result, arguments = self.request_arguments(target='opus', request_id='model-damaged')
                self.fake.patch = 'refuse'
                self.assertTrue(self.run_transition(arguments)['pending'])
                intent_path = self.directory / (em.BASE + '/requests/model-damaged.json')
                intent_bytes = intent_path.read_bytes()
                intent = ao.read(intent_path)
                self.assertEqual(intent['evidence'], result['evidence'])
                self.assertEqual(ao.digest(intent['evidence']), arguments['audit_sha256'])
                capture = self.retained_capture(self.artifact_value)
                self.assertTrue(capture.exists())
                original = capture.read_bytes()
                if case == 'missing':
                    capture.unlink()
                else:
                    capture.write_bytes(original + b'changed')
                self.fake.snapshots[SESSION]['settings'] = {'model': 'opus', 'reasoningEffort': 'max'}
                outcome = self.run_transition(arguments)
                self.assertTrue(outcome['pending'], outcome)
                self.assertIn('retained', outcome['reason'])
                self.assertEqual(self.fake.patches, 1)
                self.assertEqual(self.state()[em.POINTER_KEY]['state'], 'pending')
                self.assertFalse((self.directory / (em.BASE + '/records/model-damaged.json')).exists())
                self.assertEqual(intent_path.read_bytes(), intent_bytes)
                self.assert_retained_proof(current, 'initial room authority remains independently valid')
                if case == 'missing':
                    self.assertFalse(capture.exists())
                else:
                    self.assertEqual(capture.read_bytes(), original + b'changed')
                # The untouched private original is never a silent repair fallback.
                self.assertTrue(self.private_evidence(self.artifact_value).exists())

    def test_changed_executable_version_bytes_refuse_before_any_mutation(self):
        # The launcher bytes are rewritten to report a different version (same length, mtime
        # restored), so the recorded effective identity no longer describes what the pinned path
        # reports now. This is a truthful version-consistency negative, not a claim that the
        # launcher bytes were unchanged.
        self.qualified_room()
        self.use_family_workers()
        result, arguments = self.request_arguments(target='opus', request_id='model-version')
        self.assertTrue(result['eligible'], result.get('reason'))
        original = self.cli.read_bytes()
        info = self.cli.stat()
        self.assertIn(b'2.1.282', original)
        changed = original.replace(b'2.1.282', b'2.1.283')
        self.assertEqual(len(changed), len(original))
        self.cli.write_bytes(changed)
        os.utime(self.cli, ns=(info.st_atime_ns, info.st_mtime_ns))
        try:
            with self.assertRaisesRegex(RoomError, 'identity actually in force now'):
                self.run_transition(arguments)
        finally:
            self.cli.write_bytes(original)
            os.utime(self.cli, ns=(info.st_atime_ns, info.st_mtime_ns))
        self.assertEqual(self.fake.patches, 0)
        self.assertIsNone(self.state().get(em.POINTER_KEY))
        self.assertFalse((self.directory / em.BASE).exists())

    def test_recorded_compatibility_floor_drift_refuses(self):
        self.qualified_room()
        self.use_family_workers()
        self.recorded = {**self.recorded, 'version': '2.1.100 (Claude Code)'}
        try:
            result = self.audit('opus')
            self.assertFalse(result['eligible'])
            self.assertIn('requires Claude Code 2.1.280', result['reason'])
        finally:
            self.recorded = ao_routing.claude_evidence(str(self.cli))
        self.assertEqual(self.fake.patches, 0)
        self.assertIsNone(self.state().get(em.POINTER_KEY))
        self.assertFalse((self.directory / em.BASE).exists())

    def test_configuration_drift_refuses_before_any_mutation(self):
        self.qualified_room()
        self.use_family_workers()
        result, arguments = self.request_arguments(target='opus', request_id='model-config')
        for case, environment in (('gateway', {'ANTHROPIC_BASE_URL': 'https://gateway.invalid'}),
                                  ('alias-remap', {'ANTHROPIC_DEFAULT_OPUS_MODEL': OPUS})):
            with self.subTest(case=case):
                self.project_env.update(environment)
                with self.assertRaises(RoomError):
                    self.run_transition(arguments)
                self.project_env.clear()
                self.assertEqual(self.fake.patches, 0)
                self.assertIsNone(self.state().get(em.POINTER_KEY))
                self.assertFalse((self.directory / em.BASE).exists())

    def test_exact_child_api_error_is_termination_not_a_served_identity(self):
        self.qualified_room()
        self.use_family_workers()
        detail = 'synthetic quota (error type rate_limit, HTTP 429)'
        content = 'Agent terminated early due to an API error: ' + detail
        row = {'uuid': 'h-2', 'type': 'user', 'sessionId': NATIVE, 'cwd': str(self.repo),
               'sourceToolAssistantUUID': 'a-2',
               'message': {'role': 'user', 'content': [
                   {'type': 'tool_result', 'tool_use_id': 'toolu_1', 'content': content, 'is_error': True}]},
               'toolUseResult': 'Error: ' + content}
        self.write_transcript((self.agent_launch(), row))
        result, arguments = self.request_arguments(target='opus', request_id='model-api-error')
        self.assertEqual(result['children']['terminal_api_errors'], 1)
        outcome = self.run_transition(arguments)
        self.assertTrue(outcome['transitioned'], outcome)
        self.assertEqual(outcome['expected_model'], OPUS)
        self.assertIsNone(outcome['observed_served_model'])
        self.assertEqual(outcome['patch_result']['acknowledged'], True)
        self.assertEqual(outcome['effective_effort']['effective'], 'not evidenced')

    def test_historical_unqualified_family_pending_record_recovers_without_an_invented_expectation(self):
        self.reset(model='fable', selection=True)
        result = self.audit('opus')
        self.assertFalse(result['eligible'])
        self.assertIn('operator-selected family qualification', result['reason'])
        policy = em.effective_policy(self.service.root)[0]
        target = {'selector': {'kind': 'family', 'family': 'opus'}, 'configured_model': 'opus',
                  'reasoning_effort': 'max', 'harness': 'claude-code',
                  'qualification': copy.deepcopy(policy['families']['opus']),
                  'policy': copy.deepcopy(policy), 'policy_sha256': ao.digest(policy)}
        evidence, target = et._inspect(self.service, self.directory, self.state(), 'opus', str(self.database),
                                       str(self.transcript), expect_target=False, allow_pending=False,
                                       target_override=target)
        # The genuinely configured source of this room is the family alias 'fable', not the exact
        # bundled identifier; the historical intent must name the alias exactly as its epoch does.
        self.assertEqual(evidence['source']['configured_model'], 'fable')
        inputs = {'request_id': 'model-legacy', 'source_model': evidence['source']['configured_model'],
                  'target_model': 'opus',
                  'audit_sha256': ao.digest(evidence), 'spec_record_sha256': evidence['spec_record_sha256'],
                  'candidate_sha256': evidence['candidate']['sha256'],
                  'native_history_sha256': evidence['native']['history_sha256'],
                  'native_owner_database': str(self.database),
                  'native_transcript_path': str(self.transcript),
                  'authorization': 'The user authorized this exact engineering model transition',
                  'reason': 'A historical unqualified family request retained exactly as recorded'}
        intent = {'version': 1, 'room_id': ROOM, 'request_id': 'model-legacy', 'inputs': inputs,
                  'inputs_sha256': ao.digest(inputs), 'audit_sha256': inputs['audit_sha256'],
                  'evidence': evidence, 'before_state_sha256': et._state_digest(self.state()), 'previous': None,
                  'source': copy.deepcopy(evidence['source']), 'target': target,
                  'patch': {'method': 'PATCH', 'path': '/sessions/engineer/conversation/settings',
                            'payload': {'model': 'opus', 'reasoningEffort': 'max'}},
                  'recorded_at': 1700000000.5, 'provider_transition_receipt_sha256': None}
        em._intent(self.state(), 'model-legacy', intent)
        em.store_once(self.directory, em.BASE + '/requests/model-legacy.json', intent)
        state = self.state()
        state[em.POINTER_KEY] = {'request_id': 'model-legacy',
                                 'intent': em.BASE + '/requests/model-legacy.json',
                                 'intent_sha256': ao.digest(intent), 'state': 'pending', 'record': None,
                                 'record_sha256': None}
        em.journal(self.directory, state, allow_pending=True)
        ao.atomic(self.directory / 'state.json', state)
        outcome = et.transition(self.service, room_id=ROOM, request_id='model-legacy',
                                source_model=evidence['source']['configured_model'],
                                target_model='opus', audit_sha256=inputs['audit_sha256'],
                                spec_record_sha256=inputs['spec_record_sha256'],
                                candidate_sha256=inputs['candidate_sha256'],
                                native_history_sha256=inputs['native_history_sha256'],
                                native_owner_database=str(self.database),
                                native_transcript_path=str(self.transcript),
                                authorization=inputs['authorization'], reason=inputs['reason'])
        self.assertTrue(outcome['transitioned'], outcome)
        self.assertEqual(self.fake.patches, 1)
        self.assertIsNone(outcome['expected_model'])
        self.assertEqual(outcome['expected_model_basis'],
                         'unqualified family epoch: no pre-inference expectation')
        state = self.state()
        epoch = em.current(self.directory, state)
        self.assertFalse(epoch['family_qualified'])
        self.assertIsNone(epoch['expected_model'])
        self.assertIsNone(epoch['qualification_sha256'])
        record = ao.read(self.directory / (em.BASE + '/records/model-legacy.json'))
        self.assertEqual(record['target'], target)
        self.assertEqual(record['target']['qualification'], policy['families']['opus'])
        # No new unqualified family creation became possible, and no old request gained an expectation.
        self.assertFalse(self.audit('opus')['eligible'])

    # --- mixed root/worker proof and truthful terminal reporting ----------------------------
    def worker_artifact_for_all_families(self, revision):
        '''The enabled worker families mapped to exact models by one independent artifact.'''
        return self.artifact(revision=revision, families={
            'opus': {'expected_model': OPUS, 'source_ids': ['opus-family-doc'],
                     'minimum_claude_code_version': FLOOR},
            'sonnet': {'expected_model': WORKER_SONNET, 'source_ids': ['opus-family-doc']}})

    def mixed_room(self):
        '''Root A retained at open, independent workers B retained, new target C still private.'''
        current = self.fable_artifact(revision=3)
        self.qualified_room(model='fable', artifact=current)
        worker = self.worker_artifact_for_all_families(revision=9)
        self.use_qualified_workers(worker)
        target = self.artifact(revision=11)
        self.configure(target)
        self.artifact_value = target
        return current, worker, target

    def snapshot_reference(self, artifact):
        return {key: self.snapshot_of(artifact)[key] for key in ('sha256', 'record', 'evidence')}

    def assert_retained_proof(self, artifact, message):
        pinned = qmod.verify_reference(self.directory, self.snapshot_reference(artifact), message)
        self.assertTrue(pinned['retained_evidence'])
        self.assertEqual(pinned['sha256'], qmod.digest(artifact))
        return pinned

    def test_mixed_prospective_root_and_retained_workers_survive_worker_original_removal(self):
        current, worker, target = self.mixed_room()
        self.assert_retained_proof(current, 'current root retained proof')
        self.assertFalse(self.retained_capture(target).exists())
        self.private_evidence(worker).unlink()
        self.assertFalse(self.private_evidence(worker).exists())
        before = files_under(self.directory)
        result, arguments = self.request_arguments(target='opus', request_id='model-mixed')
        admitted = result['evidence']['qualification']
        self.assertEqual(admitted['sha256'], qmod.digest(target))
        self.assertEqual(admitted['proof'], 'private_prospective')
        self.assertEqual(admitted['worker_proof'], 'retained')
        self.assertEqual(admitted['worker_reference'], self.snapshot_reference(worker))
        self.assertEqual(admitted['worker_expected_models']['pr-opus'], OPUS)
        self.assertEqual(files_under(self.directory), before)
        self.assertEqual(self.fake.patches, 0)
        outcome = self.run_transition(arguments)
        self.assertTrue(outcome['transitioned'], outcome)
        self.assertEqual(self.fake.patches, 1)
        self.assertEqual(outcome['expected_model'], OPUS)
        self.assertEqual(outcome['qualification_sha256'], qmod.digest(target))
        self.assertEqual(outcome['observed']['settings']['model'], 'opus')
        for artifact in (current, worker, target):
            self.assert_retained_proof(artifact, 'synthetic retained proof after the mixed transition')

    def test_retained_worker_proof_corruption_refuses_even_while_private_original_remains(self):
        for case in ('missing-capture', 'changed-capture', 'missing-retained-record'):
            with self.subTest(case=case):
                current, worker, target = self.mixed_room()
                snapshot = self.snapshot_of(worker)
                capture = self.retained_capture(worker)
                original = capture.read_bytes()
                if case == 'missing-capture':
                    capture.unlink()
                elif case == 'changed-capture':
                    capture.write_bytes(original + b'changed')
                else:
                    (self.directory / snapshot['record']).unlink()
                self.assertTrue(self.private_evidence(worker).exists())
                before = files_under(self.directory)
                result = self.audit('opus')
                self.assertFalse(result['eligible'])
                self.assertIn('worker qualification retained evidence is missing or changed', result['reason'])
                self.assertEqual(self.fake.patches, 0)
                self.assertEqual(files_under(self.directory), before)
                self.assertTrue(self.private_evidence(worker).exists())
                if case == 'missing-capture':
                    self.assertFalse(capture.exists())
                elif case == 'changed-capture':
                    self.assertEqual(capture.read_bytes(), original + b'changed')
                else:
                    self.assertFalse((self.directory / snapshot['record']).exists())

    def test_no_worker_proof_pending_abandonment_and_truthful_terminal_replay(self):
        current = self.fable_artifact()
        self.qualified_room(model='fable', artifact=current)
        self.use_family_workers()
        target = self.artifact(revision=11)
        self.configure(target)
        self.artifact_value = target
        self.assertNotEqual(qmod.digest(current), qmod.digest(target))
        result, arguments = self.request_arguments(target='opus', request_id='model-no-worker')
        admitted = result['evidence']['qualification']
        self.assertEqual(admitted['proof'], 'private_prospective')
        self.assertIsNone(admitted['worker_proof'])
        self.assertIsNone(admitted['worker_reference'])
        self.assertIsNone(admitted['worker_sources'])
        self.assertIsNone(admitted['worker_families'])
        self.assertEqual(admitted['worker_expected_models'], {})
        self.fake.patch = 'refuse'
        pending = self.run_transition(arguments)
        self.assertTrue(pending['pending'], pending)
        self.assertEqual(self.fake.patches, 1)
        intent = ao.read(self.directory / (em.BASE + '/requests/model-no-worker.json'))
        self.assertEqual(intent['evidence'], result['evidence'])
        self.assertEqual(ao.digest(intent['evidence']), arguments['audit_sha256'])
        outcome = self.abandon('model-no-worker')
        self.assertTrue(outcome['abandoned'], outcome)
        self.assertFalse(outcome['pending'])
        self.assertEqual(self.fake.patches, 1)
        record = ao.read(self.directory / (em.BASE + '/records/model-no-worker.json'))
        self.assertEqual(record['outcome'], 'abandoned_unchanged')
        self.assertEqual(record['target']['configured_model'], 'opus')
        self.assertEqual(record['observed_after']['settings']['model'], 'fable')
        retry = self.run_transition(arguments)
        self.assertTrue(retry['idempotent'])
        self.assertFalse(retry['transitioned'])
        self.assertEqual(retry['configured_model'], 'fable')
        self.assertEqual(retry['observed']['settings']['model'], 'fable')
        self.assertEqual(retry['target']['configured_model'], 'opus')
        self.assertEqual(retry['expected_model'], OPUS)
        self.assertEqual(retry['qualification_sha256'], qmod.digest(self.artifact_value))
        self.assertFalse(retry['intended_target']['committed'])
        self.assertEqual(self.fake.patches, 1)

    def test_qualified_worker_pending_abandonment_replay_never_rereads_live_settings(self):
        current = self.fable_artifact()
        self.qualified_room(model='fable', artifact=current)
        worker = self.worker_artifact_for_all_families(revision=9)
        self.use_qualified_workers(worker)
        target = self.artifact(revision=11)
        self.configure(target)
        self.artifact_value = target
        result, arguments = self.request_arguments(target='opus', request_id='model-qualified-abandon')
        self.assertEqual(result['evidence']['qualification']['proof'], 'private_prospective')
        self.assertEqual(result['evidence']['qualification']['worker_proof'], 'retained')
        self.fake.patch = 'refuse'
        pending = self.run_transition(arguments)
        self.assertTrue(pending['pending'], pending)
        self.assertEqual(self.fake.patches, 1)
        intent = ao.read(self.directory / (em.BASE + '/requests/model-qualified-abandon.json'))
        self.assertEqual(intent['evidence'], result['evidence'])
        self.assertEqual(ao.digest(intent['evidence']), arguments['audit_sha256'])
        outcome = et.abandon(self.service, ROOM, 'model-qualified-abandon', str(self.database),
                             str(self.transcript),
                             'The user authorized abandoning this uncertain qualified attempt',
                             'The one refused PATCH left the retained source configuration in place')
        self.assertTrue(outcome['abandoned'], outcome)
        self.assertEqual(self.fake.patches, 1)
        # The terminal idempotent historical read reports the frozen record's own observed
        # configuration, keeps the intended target expectation/digest clearly labeled as intended,
        # and never observes the changed live settings or sends another PATCH.
        self.fake.snapshots[SESSION]['settings'] = {'model': 'claude-opus-5-6', 'reasoningEffort': 'max'}
        self.fake.snapshots[SESSION]['controller'] = 'stopped'
        calls = len(self.fake.calls)
        before = files_under(self.directory)
        retry = self.run_transition(arguments)
        self.assertTrue(retry['idempotent'])
        self.assertFalse(retry['transitioned'])
        self.assertEqual(len(self.fake.calls), calls)
        self.assertEqual(files_under(self.directory), before)
        self.assertEqual(retry['configured_model'], 'fable')
        self.assertEqual(retry['configured_model_basis'],
                         "The frozen terminal record's actual observed configuration; the intended target was "
                         'abandoned, not adopted')
        self.assertEqual(retry['observed']['settings']['model'], 'fable')
        self.assertEqual(retry['target']['configured_model'], 'opus')
        self.assertEqual(retry['expected_model'], OPUS)
        self.assertEqual(retry['qualification_sha256'], qmod.digest(target))
        self.assertFalse(retry['intended_target']['committed'])
        self.assertEqual(retry['intended_target']['configured_model'], 'opus')
        self.assertEqual(retry['intended_target']['expected_model'], OPUS)
        self.assertEqual(retry['intended_target']['qualification_sha256'], qmod.digest(target))
        self.assertIn('abandoned it without adoption', retry['intended_target']['basis'])
        self.assertIn('not adopt', retry['adoption'])
        self.assertEqual(self.fake.patches, 1)


    def test_same_family_reset_recovers_its_own_durable_abandonment_with_zero_patches(self):
        first = self.fable_artifact()
        self.qualified_room(model='fable', artifact=first)
        self.use_family_workers()
        second = self.fable_artifact(expected=NEW_FABLE, revision=4)
        self.configure(second)
        self.artifact_value = second
        result, arguments = self.request_arguments(target='fable', request_id='model-reset')
        self.assertTrue(result['reset'])
        self.assertIsNone(result['patch'])
        original_save = self.service.save

        def crash_after_pending(directory, state):
            original_save(directory, state)
            if (state.get(em.POINTER_KEY) or {}).get('state') == 'pending':
                raise OSError('synthetic crash after the actual pending publication, before _advance')

        self.service.save = crash_after_pending
        try:
            with self.assertRaises(OSError):
                self.run_transition(arguments)
        finally:
            self.service.save = original_save
        state = self.state()
        self.assertEqual(state[em.POINTER_KEY]['state'], 'pending')
        self.assertFalse((self.directory / (em.BASE + '/attempts/model-reset.json')).exists())
        self.assertEqual(self.fake.patches, 0)
        self.assertTrue(self.retained_capture(second).is_file())

        def crash_after_record(directory, state):
            raise OSError('synthetic crash after the durable abandonment record')

        self.service.save = crash_after_record
        try:
            with self.assertRaises(OSError):
                self.abandon('model-reset')
        finally:
            self.service.save = original_save
        record_path = self.directory / (em.BASE + '/records/model-reset.json')
        raw = record_path.read_bytes()
        stored = ao.read(record_path)
        self.assertEqual(stored['outcome'], em.OUTCOMES['abandoned'])
        self.assertEqual(self.state()[em.POINTER_KEY]['state'], 'pending')
        self.assertEqual(self.fake.patches, 0)

        outcome = self.run_transition(arguments)
        self.assertTrue(outcome['abandoned'], outcome)
        self.assertFalse(outcome['pending'])
        self.assertFalse(outcome['transitioned'])
        self.assertEqual(outcome['settings_patch'], 0)
        self.assertIsNone(outcome['attempt_sha256'])
        self.assertEqual(self.fake.patches, 0)
        state = self.state()
        self.assertEqual(state[em.POINTER_KEY],
                         {'request_id': 'model-reset', 'intent': em.BASE + '/requests/model-reset.json',
                          'intent_sha256': outcome['intent_sha256'], 'state': 'abandoned',
                          'record': em.BASE + '/records/model-reset.json',
                          'record_sha256': ao.digest(stored)})
        self.assertIsNone(em.journal(self.directory, state)['pending'])
        self.assertEqual(record_path.read_bytes(), raw)
        self.assertEqual(ao.read(record_path), stored)
        self.assertEqual(stored['abandonment']['authorization'],
                         'The user authorized abandoning this uncertain attempt')
        self.assertEqual(stored['abandonment']['diagnosis'],
                         'The AO PATCH response was lost; a fresh observation still shows the source')
        self.assertNotEqual(stored['abandonment']['authorization'], arguments['authorization'])
        self.assertEqual(stored['abandonment']['abandon_inputs_sha256'],
                         ao.digest({'request_id': 'model-reset', 'intent_sha256': outcome['intent_sha256'],
                                    'attempt_sha256': None, 'native_owner_database': str(self.database),
                                    'native_transcript_path': str(self.transcript),
                                    'authorization': stored['abandonment']['authorization'],
                                    'diagnosis': stored['abandonment']['diagnosis']}))
        self.assertIsInstance(stored['recorded_at'], float)
        self.assertEqual(stored['source'], {'selector': {'kind': 'family', 'family': 'fable'},
                                           'configured_model': 'fable', 'reasoning_effort': 'max'})
        self.assertEqual(stored['target']['configured_model'], 'fable')
        self.assertEqual(stored['target']['qualification']['execution_qualification']['expected_model'],
                         NEW_FABLE)
        self.assertEqual(outcome['observed']['settings']['model'], 'fable')
        self.assertEqual([epoch['configured_model'] for epoch in em.epochs(self.directory, state)], ['fable'])
        self.assertEqual(em.current(self.directory, state)['qualification_sha256'], qmod.digest(first))
        self.assert_retained_proof(first, 'the abandoned reset never replaced the room authority')
        self.assert_retained_proof(second, 'the abandoned reset left the retained target proof intact')


if __name__ == '__main__':
    unittest.main()
