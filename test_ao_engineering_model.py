"""Qualified engineering model selection, pinned policy and audited ancestry; no AO, CLI or model calls.

Every "transition" here is synthetic: the test writes complete, schema-valid v2 intent,
attempt-marker and record files through the module's own create-once writer and sets the
state pointer, exactly as the separate transition unit will. Nothing sends a PATCH.
"""
import contextlib
import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

import ao_delegates
import ao_engineering_model as em
import ao_executable_binding as binding
import ao_model_qualification as qmod
import ao_project_room as ao
import ao_routing
import ao_workflow
from room import RoomError
from test_ao_normal import Fixture

FABLE = 'claude-fable-5-1'
OPUS = 'claude-opus-5-5'
FUTURE = 'claude-opus-5-6'
ENTRY = {'harness': 'claude-code', 'reasoning_effort': 'max'}


def selector(value):
    """The selector a configured value names: a bundled family alias or an exact identifier."""
    return ({'kind': 'family', 'family': value} if value in em.BUNDLED_FAMILIES
            else {'kind': 'exact', 'model': value})


def files_under(directory, skip=('state.json',)):
    """sha256 of every room file except the state and the new transition journal."""
    result = {}
    for path in sorted(Path(directory).rglob('*')):
        relative = str(path.relative_to(directory))
        if path.is_file() and relative not in skip and not relative.startswith(em.BASE + '/'):
            result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


class PolicyTests(unittest.TestCase):
    """V2-1: the bundled table plus strictly validated private configuration."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.config = self.root / 'config.json'

    def configure(self, value):
        ao.atomic(self.config, value)

    def test_bundled_policy_is_valid_and_pins_the_default(self):
        policy, provenance = em.effective_policy(self.root)
        em.validate_policy(policy)
        self.assertEqual(policy['default_selector'], {'kind': 'family', 'family': 'fable'})
        self.assertEqual(sorted(policy['models']), [FABLE, OPUS])
        self.assertEqual(sorted(policy['families']), ['fable', 'opus', 'sonnet'])
        self.assertEqual(policy['families']['opus']['roles'], ['engineer', 'pr-opus'])
        self.assertEqual(policy['families']['opus']['minimum_claude_code_version'], '2.1.280')
        self.assertEqual(policy['families']['sonnet']['roles'], ['pr-sonnet'])
        self.assertNotIn('minimum_claude_code_version', policy['families']['fable'])
        self.assertEqual(policy['models'][OPUS]['minimum_claude_code_version'], '2.1.280')
        self.assertEqual(provenance, {'bundled_sha256': ao.digest({'families': em.BUNDLED_FAMILIES,
                                                                   'models': em.BUNDLED_MODELS}),
                                      'configuration_sha256': None, 'configured_models': []})
        self.assertEqual(em.qualification(OPUS, self.root), policy['models'][OPUS])
        self.assertEqual(em.qualification('opus', self.root), policy['families']['opus'])
        self.assertEqual(em.selection(None, self.root)['selection'], 'default')
        self.assertEqual(em.selection(None, self.root)['configured_model'], 'fable')
        self.assertEqual(em.selection(OPUS, self.root)['selection'], 'explicit')

    def test_aliases_suffixes_prefixes_and_uppercase_are_refused(self):
        for model in ('sonnet', 'haiku', 'default', 'best', 'opusplan', 'opus[1m]', 'claude-opus-latest',
                      'claude-opus-5-5[1m]', 'Claude-Opus-5-5', 'CLAUDE-OPUS-5-5', 'Opus',
                      'anthropic/claude-opus-5-5', 'us.anthropic.claude-opus-5-5', 'claude-opus-5-5 ', '',
                      None, 7, 'claude-' + 'x' * 70 + '-1'):
            with self.subTest(model=model):
                self.config.unlink(missing_ok=True)
                if model is None:  # None is the default selector, never a refusal
                    self.assertEqual(em.qualification(model, self.root), em.BUNDLED_FAMILIES['fable'])
                    continue
                with self.assertRaisesRegex(RoomError, 'Qualified engineering selectors are'):
                    em.qualification(model, self.root)
        for model in ('opus', 'fable', 'sonnet', 'default', 'claude-opus-latest', 'Claude-Opus-5-5', ''):
            with self.subTest(configured=model):  # configuration adds exact identifiers only, never aliases
                if isinstance(model, str):
                    self.configure({'engineering_models': {model: dict(ENTRY)}})
                    with self.assertRaisesRegex(RoomError, 'exact Claude identifiers'):
                        em.effective_policy(self.root)

    def test_unknown_keys_wrong_harness_effort_and_malformed_minimum_refuse(self):
        cases = [({'harness': 'claude-code', 'reasoning_effort': 'max', 'provider': 'anthropic'}, 'pins harness'),
                 ({'harness': 'codex', 'reasoning_effort': 'max'}, 'pins harness'),
                 ({'harness': 'claude-code', 'reasoning_effort': 'high'}, 'pins harness'),
                 ({'harness': 'claude-code'}, 'pins harness'),
                 ('not-an-entry', 'pins harness'),
                 ({'harness': 'claude-code', 'reasoning_effort': 'max', 'minimum_claude_code_version': '2.1'}, 'X.Y.Z'),
                 ({'harness': 'claude-code', 'reasoning_effort': 'max', 'minimum_claude_code_version': 'v2.1.280'}, 'X.Y.Z'),
                 ({'harness': 'claude-code', 'reasoning_effort': 'max', 'minimum_claude_code_version': 2}, 'X.Y.Z')]
        for entry, message in cases:
            with self.subTest(entry=entry):
                self.configure({'engineering_models': {FUTURE: entry}})
                with self.assertRaisesRegex(RoomError, message):
                    em.effective_policy(self.root)

    def test_default_outside_the_table_and_shape_violations_refuse(self):
        fable = {'roles': ['engineer'], **ENTRY}
        good = {'format': em.FORMAT, 'default_selector': {'kind': 'family', 'family': 'fable'}, 'meaning': em.MEANING,
                'families': {'fable': fable}, 'models': {FABLE: dict(ENTRY)}}
        em.validate_policy(good)
        em.validate_policy({**good, 'default_selector': {'kind': 'exact', 'model': FABLE}})
        for default in ({'kind': 'exact', 'model': OPUS}, {'kind': 'family', 'family': 'opus'}):
            with self.assertRaisesRegex(RoomError, 'default is not a qualified engineering selector'):
                em.validate_policy({**good, 'default_selector': default})
        with self.assertRaisesRegex(RoomError, 'default is not a qualified engineering selector'):
            em.validate_policy({**good, 'families': {'fable': {**fable, 'roles': ['pr-sonnet']}}})
        for default in ('fable', {'kind': 'family', 'family': 'Fable'}, {'kind': 'family'},
                        {'kind': 'exact', 'model': 'opus'}, {'kind': 'alias', 'family': 'fable'}):
            with self.assertRaisesRegex(RoomError, 'family alias or an exact Claude identifier'):
                em.validate_policy({**good, 'default_selector': default})
        with self.assertRaisesRegex(RoomError, 'Unsupported engineering model policy format'):
            em.validate_policy({**good, 'format': 'project_room_engineering_models_v1'})
        with self.assertRaisesRegex(RoomError, 'what qualification does and does not mean'):
            em.validate_policy({**good, 'meaning': '  '})
        with self.assertRaisesRegex(RoomError, 'declares exactly format'):
            em.validate_policy({**good, 'extra': 1})
        with self.assertRaisesRegex(RoomError, 'declares exactly format'):
            em.validate_policy({k: v for k, v in good.items() if k != 'families'})
        with self.assertRaisesRegex(RoomError, 'qualifies 1-32 exact models'):
            em.validate_policy({**good, 'models': {}})
        with self.assertRaisesRegex(RoomError, 'qualifies 1-8 model families'):
            em.validate_policy({**good, 'families': {}})
        for name, entry, message in (('Fable', fable, 'lowercase'), ('claude-fable', fable, 'lowercase'),
                                     ('fable', {**fable, 'roles': []}, 'supported roles'),
                                     ('fable', {**fable, 'roles': ['engineer', 'engineer']}, 'supported roles'),
                                     ('fable', {**fable, 'roles': ['reviewer']}, 'supported roles'),
                                     ('fable', {**fable, 'harness': 'codex'}, 'pins harness'),
                                     ('fable', {**fable, 'reasoning_effort': 'high'}, 'pins harness'),
                                     ('fable', {**fable, 'resolved_model': FABLE}, 'pins harness'),
                                     ('fable', {**fable, 'minimum_claude_code_version': '2.1'}, 'X.Y.Z')):
            with self.subTest(name=name, entry=entry), self.assertRaisesRegex(RoomError, message):
                em.validate_policy({**good, 'families': {name: entry}})
        many = {'claude-many-%d-0' % n: dict(ENTRY) for n in range(33)}
        with self.assertRaisesRegex(RoomError, 'qualifies 1-32 exact models'):
            em.validate_policy({**good, 'models': many})

    def test_configuration_adds_a_future_exact_model_without_source_edits(self):
        self.configure({'engineering_models': {FUTURE: dict(ENTRY)}})
        policy, provenance = em.effective_policy(self.root)
        self.assertEqual(sorted(policy['models']), [FABLE, OPUS, FUTURE])
        self.assertEqual(provenance['configured_models'], [FUTURE])
        self.assertEqual(provenance['configuration_sha256'], ao.digest({FUTURE: dict(ENTRY)}))
        self.assertEqual(provenance['bundled_sha256'], ao.digest({'families': em.BUNDLED_FAMILIES,
                                                                  'models': em.BUNDLED_MODELS}))
        self.assertEqual(em.qualification(FUTURE, self.root), dict(ENTRY))
        self.assertEqual(policy['default_selector'], {'kind': 'family', 'family': 'fable'})  # never configurable
        self.assertEqual(policy['families'], em.BUNDLED_FAMILIES)  # configuration never adds or alters a family

    def test_configuration_may_repeat_but_never_redefine_a_bundled_qualification(self):
        self.configure({'engineering_models': {OPUS: dict(em.BUNDLED_MODELS[OPUS])}})
        self.assertEqual(em.effective_policy(self.root)[0]['models'][OPUS], em.BUNDLED_MODELS[OPUS])
        for entry in (dict(ENTRY), {**ENTRY, 'minimum_claude_code_version': '1.0.0'}):
            self.configure({'engineering_models': {OPUS: entry}})
            with self.assertRaisesRegex(RoomError, 'never altered, weakened or removed'):
                em.effective_policy(self.root)

    def test_non_object_and_over_bound_configuration_refuse(self):
        for value in ([FUTURE], 'claude-opus-5-6', 7, None):
            self.configure({'engineering_models': value})
            with self.assertRaisesRegex(RoomError, 'unreadable or malformed'):
                em.effective_policy(self.root)
        self.configure({'engineering_models': {'claude-many-%d-0' % n: dict(ENTRY) for n in range(31)}})
        with self.assertRaisesRegex(RoomError, 'qualifies 1-32 exact models'):
            em.effective_policy(self.root)

    def test_unreadable_configuration_refuses_and_absence_is_bundled_only(self):
        self.assertEqual(em.effective_policy(self.root)[1]['configuration_sha256'], None)
        self.config.write_text('{"engineering_models": {')
        with self.assertRaisesRegex(RoomError, 'unreadable or malformed'):
            em.effective_policy(self.root)
        self.config.write_text('[]')
        with self.assertRaisesRegex(RoomError, 'unreadable or malformed'):
            em.effective_policy(self.root)
        ao.atomic(self.config, {'engineering_models': {FUTURE: dict(ENTRY)}})
        self.config.chmod(0o000)
        self.addCleanup(self.config.chmod, 0o600)
        with self.assertRaisesRegex(RoomError, 'unreadable or malformed'):
            em.effective_policy(self.root)
        self.config.chmod(0o600)
        link = self.root / 'linked'
        link.mkdir()
        (link / 'config.json').symlink_to(self.config)
        with self.assertRaisesRegex(RoomError, 'unreadable or malformed'):
            em.effective_policy(link)
        # An unrelated private key never becomes policy input.
        ao.atomic(self.config, {'engineering_preference': 'fable', 'claude_bin': '/usr/bin/claude'})
        self.assertEqual(em.effective_policy(self.root)[1]['configured_models'], [])


class ModelFixture(Fixture):
    """Normal-room fixture plus helpers that build complete v2 journal artifacts.

    Modern preparations here carry independently selected source-qualified worker evidence even when
    the room's own ROOT is a historical exact or unqualified v2 policy: the scoped helpers below
    select the worker pointer for exactly one preparation and restore the active configuration.
    """

    WORKER_FLOOR = '2.1.280'
    WORKER_OPUS = 'claude-opus-5-5'
    WORKER_SONNET = 'claude-sonnet-5-5'

    def open_model(self, feature='normal', model=None, provider='none', **kwargs):
        return self.service.ao_room_open(str(self.repo), feature, 'project', 'User authorized this feature',
                                         'http://127.0.0.1:1234', delegate_provider=provider,
                                         engineering_model=model, **kwargs)

    def room_id_for(self, feature):
        return 'ao-' + ao.digest({'path': str(self.repo), 'feature': feature})[:24]

    def install_claude(self, name, version):
        path = self.root / name
        path.write_text('#!/bin/sh\nprintf "' + version + '\\n"\n')
        path.chmod(0o700)
        ao.atomic(self.home / 'config.json', {'claude_bin': str(path), 'claude_config_dir': str(self.claude_env)})
        return path

    def worker_qualification_artifact(self, opus=None, sonnet=None, revision=1):
        """One synthetic private artifact mapping both enabled worker families to exact models.

        A fresh source-qualified preparation independently selects this artifact through the private
        configuration key exactly as production does; nothing here bypasses admission. The room's own
        root epoch, its pinned policy record and its opening selection stay untouched: only the
        preparation selects these workers.
        """
        path = self.root / 'worker-qualification-evidence.txt'
        path.write_text('Synthetic operator-selected worker documentation excerpt.')
        path.chmod(0o600)
        descriptor = {'id': 'worker-family-doc', 'uri': 'https://docs.invalid.example/claude/worker-family',
                      'captured_at': '2026-06-01T00:00:00Z',
                      'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'evidence_file': str(path)}
        artifact = {'format': qmod.FORMAT, 'revision': revision, 'qualified_at': '2026-06-02T00:00:00Z',
                    'scope': copy.deepcopy(qmod.SCOPE),
                    'families': {'opus': {'expected_model': opus or self.WORKER_OPUS,
                                          'source_ids': ['worker-family-doc'],
                                          'minimum_claude_code_version': self.WORKER_FLOOR},
                                 'sonnet': {'expected_model': sonnet or self.WORKER_SONNET,
                                            'source_ids': ['worker-family-doc']}},
                    'sources': [descriptor]}
        return artifact

    @contextlib.contextmanager
    def worker_preparation(self, artifact=None):
        """Temporarily select a qualified worker artifact for one modern preparation, then restore.

        The room's own root epoch, its opening selection and its pinned policy record are untouched:
        only the private pointer the preparation selects its workers through is replaced for the
        actual call and restored immediately afterwards. This is a scoped fixture, never a global
        admission bypass and never a rewrite of an old ROOT source. Each selected artifact keeps its
        own path keyed by its digest, so a nested preparation can never overwrite the outer bytes.
        """
        artifact = self.worker_qualification_artifact() if artifact is None else artifact
        sha256 = qmod.digest(artifact)
        path = self.root / ('worker-qualification-' + sha256 + '.json')
        path.write_text(json.dumps(artifact, indent=2, sort_keys=True))
        path.chmod(0o600)
        config = self.home / 'ao' / 'config.json'
        value = ao.read(config) if config.exists() else {}
        had, previous = 'family_qualification' in value, value.get('family_qualification')
        value['family_qualification'] = {'path': str(path), 'sha256': sha256}
        ao.atomic(config, value)
        try:
            yield artifact
        finally:
            value = ao.read(config) if config.exists() else {}
            if had:
                value['family_qualification'] = previous
            else:
                value.pop('family_qualification', None)
            ao.atomic(config, value)

    def worker_qualification_applies(self, artifact):
        """True when an artifact maps every enabled worker family a modern preparation requires."""
        families = artifact.get('families') if isinstance(artifact, dict) else None
        return isinstance(families, dict) and all(family in families
                                                  for family in ao_routing.WORKER_FAMILIES.values())

    def prepare(self):
        """This suite's one modern preparation, carrying independently selected worker evidence.

        Every modern preparation here must satisfy the source-qualified worker requirement even when
        the test's ROOT is a historical exact or unqualified v2 policy: the scoped worker qualification
        is selected for exactly this call and the intended private configuration is restored
        immediately afterwards. An explicitly configured applicable worker artifact is preserved;
        missing or inapplicable explicit evidence refuses instead of silently becoming a valid default.
        """
        config = self.home / 'ao' / 'config.json'
        value = ao.read(config) if config.exists() else {}
        pointer = value.get('family_qualification')
        if pointer is not None:
            qualification = qmod.load(pointer)
            if not self.worker_qualification_applies(qualification['artifact']):
                raise ao.RoomError('The configured family qualification does not map every enabled worker family')
            with self.worker_preparation(qualification['artifact']):
                return self.service.ao_room_prepare(self.room, str(self.repo))
        with self.worker_preparation():
            return self.service.ao_room_prepare(self.room, str(self.repo))

    def historical_preparation(self):
        """Record the retained pre-qualification v2 family bytes for a deliberately old executable.

        A deliberately old or noncompatible executable cannot carry a modern source-qualified worker
        preparation: the qualified worker floors would refuse it before the case reaches the
        historical root compatibility boundary or the executable-binding lane it exercises. The real
        preparation is therefore made once under the scoped worker qualification with the fixture's
        floor-compatible executable, and the recorded preparation is then rewritten to the exact
        historical v2 family bytes: the same worktree files rendered for the historical family
        selectors and the configured executable's own freshly probed evidence. The room's root
        selection, its pinned policy record and the caller's configuration are never rewritten.
        """
        config = self.home / 'config.json'
        value = ao.read(config)
        if value.get('claude_bin') != str(self.fixture_claude):
            ao.atomic(config, {**value, 'claude_bin': str(self.fixture_claude)})
        try:
            self.prepare()
        finally:
            ao.atomic(config, value)
        directory, state = self.directory(), self.state()
        path = directory / state['preparation']
        prepared = ao.read(path)
        routing, worktree = prepared['routing'], Path(prepared['worktree'])
        for name, model in ao_routing.FAMILY_AGENTS.items():
            relative = '.claude/agents/' + name + '.md'
            data = ao_routing.agent_definition(name, model).encode()
            (worktree / relative).write_bytes(data)
            routing['files'][relative] = ao.digest(data)
        routing['version'] = 2
        routing['agents'] = dict(ao_routing.FAMILY_AGENTS)
        routing['agent_selection'] = {name: dict(item) for name, item in ao_routing.AGENT_SELECTION.items()}
        routing['agent_identity_basis'] = ao_routing.AGENT_IDENTITY_BASIS
        routing.pop('worker_qualification', None)
        routing['claude'] = ao_routing.claude_evidence(ao.read(config).get('claude_bin'))
        ao.atomic(path, prepared)
        state['preparation_sha256'] = ao.digest(prepared)
        ao.atomic(directory / 'state.json', state)
        return prepared

    def bind_at(self, model=None):
        model = model or ao_workflow.FABLE_MODEL  # the default room's configured family alias
        self.fake.snapshots['engineer']['settings']['model'] = model
        self.prepare()
        self.service.ao_room_bind(self.room, 'engineer', 'engineer', model, 'max')
        self.service.ao_room_bind(self.room, 'reviewer', 'reviewer', 'astra', 'max')

    def selection_record(self, state=None):
        return (state or self.state())[em.SELECTION_KEY]

    # --- synthetic v2 journal artifacts -------------------------------------------
    def parts(self, request_id='switch-model', source_model=FABLE, target_model=OPUS, state=None,
              approval=None, outcome='committed', boundary=None, previous=None, drop_marker=False,
              intent_changes=None, evidence_changes=None, evidence_drop=None, record_changes=None,
              target_changes=None, owner_changes=None):
        """A complete v3 journal attempt. Equal source/target values build a family resolution reset."""
        state = copy.deepcopy(state or self.state())
        state.pop(em.POINTER_KEY, None)
        engineer, policy = state['bindings']['engineer'], em.effective_policy(self.service.root)[0]
        chosen = selector(target_model)
        entry = policy['families' if chosen['kind'] == 'family' else 'models'][target_model]
        target = {'selector': chosen, 'configured_model': target_model, 'reasoning_effort': 'max',
                  'harness': 'claude-code', 'qualification': copy.deepcopy(entry),
                  'policy': copy.deepcopy(policy), 'policy_sha256': ao.digest(policy)}
        target.update(target_changes or {})
        reset = target_model == source_model
        source = {'selector': selector(source_model), 'configured_model': source_model, 'reasoning_effort': 'max'}
        settings = {'model': source_model, 'reasoningEffort': 'max'}
        if approval is not None:
            settings['approvalMode'] = approval
        ordered = sorted(state['requests'].values(), key=lambda r: r['created_order'])
        owner = {'id': engineer['session_id'], 'project_id': 'project', 'provider_conversation_id': 'native-uuid',
                 'ao_conversation_id': engineer['conversation_id'], 'active_branch_id': engineer['branch_id'],
                 'controller_generation': 'generation-1', 'activity_state': 'idle', 'workspace_path': str(self.repo)}
        owner.update(owner_changes or {})
        history_sha, transcript_sha, candidate_sha = 'a' * 64, 'b' * 64, 'c' * 64
        before = ao.digest(state)
        evidence = {'version': 1, 'room_id': state['room_id'], 'state_sha256': before,
                    'source': copy.deepcopy(source),
                    'target': {'selector': copy.deepcopy(target['selector']),
                               'configured_model': target['configured_model'],
                               'qualification': copy.deepcopy(target['qualification']),
                               'policy_sha256': target['policy_sha256']},
                    'engineer': copy.deepcopy(engineer),
                    'requests': [{'request_id': r['request_id'], 'created_order': r['created_order']} for r in ordered],
                    'spec_record_sha256': state['spec_record_sha256'],
                    'candidate': {'sha256': candidate_sha, 'path': str(self.repo)},
                    'native': {'session_id': engineer['session_id'], 'conversation_id': engineer['conversation_id'],
                               'branch_id': engineer['branch_id'], 'controller': 'ready', 'settings': settings,
                               'history_sha256': history_sha, 'transcript_sha256': transcript_sha},
                    'native_owner': owner,
                    'journals': {'provider_transition': state.get('provider_transition'),
                                 'executable_binding': state.get('executable_binding')}}
        evidence.update(evidence_changes or {})
        evidence.pop(evidence_drop, None)
        audit = ao.digest(evidence)
        inputs = {'request_id': request_id, 'source_model': source_model, 'target_model': target['configured_model'],
                  'audit_sha256': audit, 'spec_record_sha256': state['spec_record_sha256'],
                  'candidate_sha256': candidate_sha, 'native_history_sha256': history_sha,
                  'native_owner_database': str(self.root / 'fake.db'),
                  'native_transcript_path': str(self.root / 'native-uuid.jsonl'),
                  'authorization': 'User authorized this exact engineering model transition',
                  'reason': 'The qualified orchestrator model replaces the original for this room'}
        payload = {'model': target['configured_model'], 'reasoningEffort': 'max'}
        if approval:
            payload['approvalMode'] = approval
        patch_call = None if reset else {'method': 'PATCH', 'payload': payload,
                                         'path': '/sessions/' + engineer['session_id'] + '/conversation/settings'}
        intent = {'version': 1, 'room_id': state['room_id'], 'request_id': request_id, 'inputs': inputs,
                  'inputs_sha256': ao.digest(inputs), 'audit_sha256': audit, 'evidence': evidence,
                  'before_state_sha256': before, 'previous': previous, 'source': copy.deepcopy(source),
                  'target': target, 'patch': patch_call, 'recorded_at': 1700000000.0,
                  'provider_transition_receipt_sha256': (state.get('provider_transition') or {}).get('receipt_sha256')}
        intent.update(intent_changes or {})
        intent_sha = ao.digest(intent)
        marker = None if drop_marker or reset else {'version': 1, 'room_id': state['room_id'], 'request_id': request_id,
                                                    'intent_sha256': intent_sha, 'patch': copy.deepcopy(patch_call),
                                                    'started_at': 1700000001.0}
        observed_before = {'settings': copy.deepcopy(settings), 'controller': 'ready', 'history_sha256': history_sha,
                           'transcript_sha256': transcript_sha, 'native_owner': copy.deepcopy(owner)}
        after = copy.deepcopy(settings)
        applied = outcome == 'committed' and not reset
        if applied:
            after['model'] = target['configured_model']
        record = {'version': 1, 'room_id': state['room_id'], 'request_id': request_id, 'intent_sha256': intent_sha,
                  'attempt_sha256': None if marker is None else ao.digest(marker), 'outcome': outcome,
                  'patch_result': {'acknowledged': applied, 'error': None,
                                   'response_sha256': 'd' * 64 if applied else None},
                  'observed_before': observed_before,
                  'observed_after': {**copy.deepcopy(observed_before), 'settings': after},
                  'source': copy.deepcopy(source), 'target': copy.deepcopy(target),
                  'boundary_order': len(evidence.get('requests') or []) if boundary is None else boundary,
                  'recorded_at': 1700000002.0}
        if outcome == 'abandoned_unchanged':
            record['abandonment'] = {'authorization': 'User authorized abandoning this uncertain attempt',
                                     'diagnosis': 'A fresh read-only observation shows the exact recorded source settings',
                                     'abandon_inputs_sha256': 'e' * 64}
        record.update(record_changes or {})
        return {'intent': intent, 'intent_sha256': intent_sha, 'marker': marker,
                'record': record, 'record_sha256': ao.digest(record)}

    def pointer_for(self, parts, pointer_state):
        rid = parts['intent']['request_id']
        return {'request_id': rid, 'intent': '%s/requests/%s.json' % (em.BASE, rid), 'state': pointer_state,
                'intent_sha256': parts['intent_sha256'],
                'record': None if pointer_state == 'pending' else '%s/records/%s.json' % (em.BASE, rid),
                'record_sha256': None if pointer_state == 'pending' else parts['record_sha256']}

    def publish(self, parts, pointer_state='committed', record=True, marker=True, save=True):
        """Write the create-once artifacts and (optionally) publish the state pointer."""
        directory, rid = self.directory(), parts['intent']['request_id']
        em.store_once(directory, '%s/requests/%s.json' % (em.BASE, rid), parts['intent'])
        if marker and parts['marker'] is not None:
            em.store_once(directory, '%s/attempts/%s.json' % (em.BASE, rid), parts['marker'])
        if record:
            em.store_once(directory, '%s/records/%s.json' % (em.BASE, rid), parts['record'])
        pointer = self.pointer_for(parts, pointer_state)
        if save:
            state = self.state()
            state[em.POINTER_KEY] = pointer
            ao.atomic(directory / 'state.json', state)
        return pointer

    def clear_journal(self):
        for kind in em.KINDS:
            folder = self.directory() / em.BASE / kind
            for child in folder.iterdir() if folder.is_dir() else ():
                child.unlink()

    def proposed(self, parts, pointer_state='committed', **kwargs):
        """Write the artifacts and return (state-with-pointer) without saving state.json."""
        pointer = self.publish(parts, pointer_state, save=False, **kwargs)
        return {**self.state(), em.POINTER_KEY: pointer}

    def assert_refused(self, parts, message='Unclaimed, missing or inconsistent', pointer_state='committed',
                       journal=True, **kwargs):
        state = self.proposed(parts, pointer_state, **kwargs)
        try:
            if journal:
                with self.assertRaisesRegex(ao.RoomError, message):
                    em.journal(self.directory(), state, allow_pending=True)
            with self.assertRaisesRegex(ao.RoomError, message):
                em.epochs(self.directory(), state)
        finally:
            self.clear_journal()

    def assert_accepted(self, parts, pointer_state='committed', **kwargs):
        state = self.proposed(parts, pointer_state, **kwargs)
        try:
            return em.journal(self.directory(), state), em.epochs(self.directory(), state)
        finally:
            self.clear_journal()


class SelectionTests(ModelFixture):
    """V2-2: a pinned selection snapshot bound to an immutable policy record."""

    def test_default_room_records_the_pinned_policy_and_an_immutable_record(self):
        self.room = self.open_model('default-room')['room_id']
        record = self.selection_record()
        policy, provenance = em.effective_policy(self.service.root)
        self.assertEqual(record['version'], 2)
        self.assertEqual(record['selector'], {'kind': 'family', 'family': 'fable'})
        self.assertEqual(record['configured_model'], 'fable')
        self.assertNotIn('model', record)  # an alias is intent, never recorded as an exact model
        self.assertEqual(record['selection'], 'default')
        self.assertEqual(record['harness'], 'claude-code')
        self.assertEqual(record['reasoning_effort'], 'max')
        self.assertEqual(record['qualification'], policy['families']['fable'])
        self.assertEqual(record['policy'], policy)
        self.assertEqual(record['policy_sha256'], ao.digest(policy))
        self.assertEqual(record['provenance'], provenance)
        self.assertEqual(record['policy_record'], 'engineering-model/policy-' + ao.digest(policy) + '.json')
        self.assertIn('not provider availability', record['basis'])
        saved = self.directory() / record['policy_record']
        self.assertEqual(json.loads(saved.read_bytes()), policy)
        self.assertEqual(saved.read_bytes(), em.json_bytes(policy))
        self.assertEqual(stat.S_IMODE(saved.lstat().st_mode), 0o600)
        self.assertEqual(em.initial(self.directory(), self.state()),
                         {'selector': {'kind': 'family', 'family': 'fable'}, 'configured_model': 'fable',
                          'model': 'fable', 'reasoning_effort': 'max', 'harness': 'claude-code',
                          'qualification': policy['families']['fable'], 'policy_sha256': ao.digest(policy),
                          'source': 'room_selection'})
        # Reopening the identical room is idempotent and rewrites nothing.
        before = files_under(self.directory())
        self.assertEqual(self.open_model('default-room')['room_id'], self.room)
        self.assertEqual(self.open_model('default-room', model='fable')['room_id'], self.room)
        with self.assertRaisesRegex(ao.RoomError, 'Existing room engineering model is immutable'):
            self.open_model('default-room', model=FABLE)  # the exact member is a different selector
        self.assertEqual(files_under(self.directory()), before)

    def test_explicit_opus_selection_is_pinned(self):
        self.install_claude('claude-qualified', '2.1.280 (Claude Code)')
        self.room = self.open_model('opus-room', model=OPUS)['room_id']
        record = self.selection_record()
        self.assertEqual((record['selector'], record['configured_model'], record['selection']),
                         ({'kind': 'exact', 'model': OPUS}, OPUS, 'explicit'))
        self.assertEqual(record['qualification']['minimum_claude_code_version'], '2.1.280')
        self.assertEqual(em.current_qualification(self.directory(), self.state()), record['qualification'])

    def test_unqualified_model_is_refused_before_any_room_directory_exists(self):
        for model in ('sonnet', 'haiku', 'claude-opus-latest', 'claude-opus-5-9', 'CLAUDE-OPUS-5-5'):
            with self.subTest(model=model):
                with self.assertRaisesRegex(ao.RoomError, 'Qualified engineering selectors are'):
                    self.open_model('unqualified-' + model.lower().replace('/', '-'), model=model)
                self.assertFalse((self.home / 'ao' / 'rooms' /
                                  self.room_id_for('unqualified-' + model.lower().replace('/', '-'))).exists())

    def test_reopening_with_another_model_is_refused_and_never_migrates(self):
        self.room = self.open_model('immutable-room')['room_id']
        before = (self.directory() / 'state.json').read_bytes()
        with self.assertRaisesRegex(ao.RoomError, 'Existing room engineering model is immutable'):
            self.open_model('immutable-room', model=OPUS)
        self.assertEqual((self.directory() / 'state.json').read_bytes(), before)

    def test_legacy_room_without_the_key_stays_legacy(self):
        self.room = self.open_model('legacy-room')['room_id']
        directory, state = self.directory(), self.state()
        record = state.pop(em.SELECTION_KEY)
        (directory / record['policy_record']).unlink()
        (directory / 'engineering-model').rmdir()
        ao.atomic(directory / 'state.json', state)
        self.assertEqual(em.initial(directory, self.state()),
                         {'selector': {'kind': 'exact', 'model': FABLE}, 'configured_model': FABLE, 'model': FABLE,
                          'reasoning_effort': 'max', 'harness': 'claude-code',
                          'qualification': {}, 'policy_sha256': None, 'source': 'legacy_default'})
        before = (directory / 'state.json').read_bytes()
        self.assertEqual(self.open_model('legacy-room')['room_id'], self.room)
        self.assertEqual((directory / 'state.json').read_bytes(), before)
        self.assertNotIn(em.SELECTION_KEY, self.state())
        self.assertEqual(self.service.ao_room_status(self.room)['engineering_model']['initial']['source'],
                         'legacy_default')
        self.spec()
        with self.assertRaisesRegex(ao.RoomError, 'configured Claude engineering model value'):
            self.bind_at('fable')  # the new default alias never reinterprets a legacy room
        self.bind_at(FABLE)  # a legacy room still binds its historical exact default with no minimum
        self.assertEqual(self.state()['bindings']['engineer']['model'], FABLE)
        self.assertNotIn(em.SELECTION_KEY, self.state())

    def test_astra_led_room_configures_no_engineering_model(self):
        with self.assertRaisesRegex(ao.RoomError, 'Astra-led exception configures no engineering orchestrator model'):
            self.open_model('astra-room', model=OPUS, workflow='astra_led',
                            exception_authorization='Actual user one-off choice')
        value = self.open_model('astra-room', workflow='astra_led',
                                exception_authorization='Actual user one-off choice')
        self.assertNotIn(em.SELECTION_KEY, ao.read(Path(value['room_path']) / 'state.json'))
        with self.assertRaisesRegex(ao.RoomError, 'Existing room engineering model is immutable'):
            self.open_model('astra-room', model=FABLE)

    def test_configuration_adds_a_future_model_and_a_pinned_room_ignores_later_edits(self):
        ao.atomic(self.home / 'ao' / 'config.json', {'engineering_models': {FUTURE: dict(ENTRY)}})
        self.room = self.open_model('future-room', model=FUTURE)['room_id']
        record = self.selection_record()
        self.assertEqual(record['selector'], {'kind': 'exact', 'model': FUTURE})
        self.assertEqual(record['provenance']['configured_models'], [FUTURE])
        pinned = copy.deepcopy(record)
        # Removing the configuration afterwards never rewrites this room's pinned snapshot.
        ao.atomic(self.home / 'ao' / 'config.json', {})
        self.assertEqual(self.selection_record(), pinned)
        self.assertEqual(em.initial(self.directory(), self.state())['configured_model'], FUTURE)
        self.assertEqual(self.service.ao_room_status(self.room)['engineering_model']['configured']['model'], FUTURE)
        self.spec()
        self.bind_at(FUTURE)
        self.assertEqual(self.state()['bindings']['engineer']['model'], FUTURE)
        with self.assertRaisesRegex(ao.RoomError, 'Qualified engineering selectors are'):
            self.open_model('future-room-2', model=FUTURE)

    def test_malformed_configuration_refuses_a_new_room(self):
        for value in ({'opus': dict(ENTRY)}, {FUTURE: {'harness': 'codex', 'reasoning_effort': 'max'}},
                      {FUTURE: {**ENTRY, 'provider': 'anthropic'}}, [FUTURE], 'text',
                      {OPUS: dict(ENTRY)}):
            with self.subTest(value=value):
                ao.atomic(self.home / 'ao' / 'config.json', {'engineering_models': value})
                with self.assertRaises(ao.RoomError):
                    self.open_model('configured-room')
                self.assertFalse((self.home / 'ao' / 'rooms' / self.room_id_for('configured-room')).exists())

    # --- review finding 1: the saved qualification is bound to its immutable record ---
    def test_weakened_qualification_with_the_original_policy_digest_is_refused(self):
        self.install_claude('claude-old', '2.1.268 (Claude Code)')
        self.room = self.open_model('finding-one', model=OPUS)['room_id']
        self.spec()
        # A deliberately old executable cannot carry the modern worker preparation; the recorded
        # historical bytes keep the policy-inconsistency refusal this case exercises.
        self.historical_preparation()
        self.fake.snapshots['engineer']['settings']['model'] = OPUS
        directory, state = self.directory(), self.state()
        # The original, unchanged policy digest with a silently weakened saved qualification.
        state[em.SELECTION_KEY]['qualification'].pop('minimum_claude_code_version')
        ao.atomic(directory / 'state.json', state)
        self.assertEqual(self.state()[em.SELECTION_KEY]['policy_sha256'],
                         em.effective_policy(self.service.root)[0] and ao.digest(em.effective_policy(self.service.root)[0]))
        with self.assertRaisesRegex(ao.RoomError, 'inconsistent with its pinned policy'):
            em.initial(directory, self.state())
        with self.assertRaisesRegex(ao.RoomError, 'inconsistent with its pinned policy'):
            self.service.ao_room_bind(self.room, 'engineer', 'engineer', OPUS, 'max')
        self.assertNotIn('engineer', self.state()['bindings'])
        self.assertIn('inconsistent with its pinned policy',
                      self.service.ao_room_status(self.room)['engineering_model']['error'])

    def test_tampered_policy_record_or_digest_is_refused(self):
        self.room = self.open_model('finding-one-record')['room_id']
        directory = self.directory()
        record = self.selection_record()
        saved = directory / record['policy_record']
        forged = copy.deepcopy(record['policy'])
        forged['models']['claude-forged-9-9'] = dict(ENTRY)
        saved.write_bytes(em.json_bytes(forged))
        with self.assertRaisesRegex(ao.RoomError, 'inconsistent with its pinned policy'):
            em.initial(directory, self.state())
        saved.write_bytes(em.json_bytes(record['policy']))
        em.initial(directory, self.state())
        saved.unlink()
        with self.assertRaisesRegex(ao.RoomError, 'inconsistent with its pinned policy'):
            em.initial(directory, self.state())
        saved.write_bytes(em.json_bytes(record['policy']))
        for change in ({'policy_sha256': 'f' * 64},
                       {'policy_record': 'engineering-model/policy-' + 'f' * 64 + '.json'},
                       {'policy': {**record['policy'], 'models': {FABLE: dict(ENTRY)}}},
                       {'version': 1}, {'version': 3}, {'harness': 'codex'}, {'reasoning_effort': 'high'},
                       {'selection': 'inferred'}, {'basis': ''}, {'provenance': 'none'},
                       {'configured_model': FABLE}, {'configured_model': 'opus'},
                       {'selector': {'kind': 'family', 'family': 'opus'}},
                       {'selector': {'kind': 'family', 'family': 'sonnet'}},
                       {'selector': {'kind': 'exact', 'model': FABLE}, 'configured_model': FABLE},
                       {'selector': 'fable'}, {'model': FABLE}):
            with self.subTest(change=change):
                state = self.state()
                state[em.SELECTION_KEY] = {**record, **change}
                with self.assertRaisesRegex(ao.RoomError, 'inconsistent with its pinned policy'):
                    em.initial(directory, state)
        state = self.state()
        state[em.SELECTION_KEY] = {k: v for k, v in record.items() if k != 'provenance'}
        with self.assertRaisesRegex(ao.RoomError, 'inconsistent with its pinned policy'):
            em.initial(directory, state)


class BindTests(ModelFixture):
    """V2-2/B: bind admits exactly the room's configured model and its executable minimum."""

    def test_fable_room_accepts_only_fable(self):
        self.room = self.open_model('bind-fable')['room_id']
        self.spec()
        self.fake.snapshots['engineer']['settings']['model'] = 'fable'
        self.prepare()
        for model in (OPUS, 'opus', 'astra', FABLE, 'claude-fable-5-2'):
            with self.subTest(model=model), self.assertRaisesRegex(ao.RoomError, 'configured Claude engineering model value'):
                self.service.ao_room_bind(self.room, 'engineer', 'engineer', model, 'max')
        with self.assertRaisesRegex(ao.RoomError, 'configured Claude engineering model value'):
            self.service.ao_room_bind(self.room, 'engineer', 'engineer', 'fable', 'high')
        self.service.ao_room_bind(self.room, 'engineer', 'engineer', 'fable', 'max')
        self.assertEqual(self.state()['bindings']['engineer']['fable_reason'], 'Designated Fable engineering role')

    def test_opus_room_refuses_fable_and_requires_the_qualified_executable(self):
        self.install_claude('claude-2-1-268', '2.1.268 (Claude Code)')
        self.room = self.open_model('bind-opus', model=OPUS)['room_id']
        self.spec()
        self.fake.snapshots['engineer']['settings']['model'] = OPUS
        # A deliberately old executable cannot carry the modern source-qualified worker preparation:
        # the recorded historical v2 bytes keep the root compatibility boundary this case exercises.
        self.historical_preparation()
        for model in (FABLE, 'opus', 'fable'):
            with self.subTest(model=model), self.assertRaisesRegex(ao.RoomError, 'configured Claude engineering model value'):
                self.service.ao_room_bind(self.room, 'engineer', 'engineer', model, 'max')
        with self.assertRaisesRegex(ao.RoomError, 'requires Claude Code 2.1.280 or newer'):
            self.service.ao_room_bind(self.room, 'engineer', 'engineer', OPUS, 'max')
        self.assertNotIn('engineer', self.state()['bindings'])

    def test_opus_room_refuses_an_unparseable_executable_version(self):
        self.install_claude('claude-unparseable', '0.0-fake (Claude Code)')
        self.room = self.open_model('bind-unparseable', model=OPUS)['room_id']
        self.spec()
        self.fake.snapshots['engineer']['settings']['model'] = OPUS
        # A deliberately noncompatible executable cannot carry the modern worker preparation: the
        # recorded historical bytes keep the root compatibility boundary this case exercises.
        self.historical_preparation()
        with self.assertRaisesRegex(ao.RoomError, 'requires Claude Code 2.1.280 or newer'):
            self.service.ao_room_bind(self.room, 'engineer', 'engineer', OPUS, 'max')
        self.assertIsNone(em.parse_version('0.0-fake (Claude Code)'))
        self.assertEqual(em.parse_version('2.1.280 (Claude Code)'), (2, 1, 280))
        self.assertEqual(em.parse_version('2.1.280'), (2, 1, 280))
        self.assertIsNone(em.parse_version('2.1'))
        self.assertIsNone(em.parse_version(None))

    def test_opus_room_binds_on_the_qualified_executable(self):
        self.install_claude('claude-2-1-280', '2.1.280 (Claude Code)')
        self.room = self.open_model('bind-opus-ok', model=OPUS)['room_id']
        self.spec()
        self.bind_at(OPUS)
        state = self.state()
        self.assertEqual(state['bindings']['engineer']['model'], OPUS)
        self.assertEqual(em.effective_binding(self.directory(), state)['model'], OPUS)
        self.assertEqual(em.current(self.directory(), state)['model'], OPUS)

    def test_nested_worker_qualification_preserves_selected_bytes_and_pointer(self):
        self.room = self.open_model('nested-worker-qualification')['room_id']
        self.spec()
        selected = self.worker_qualification_artifact(revision=9)
        config = self.home / 'ao' / 'config.json'
        with self.worker_preparation(selected):
            pointer = ao.read(config)['family_qualification']
            self.assertEqual(pointer['sha256'], qmod.digest(selected))
            prepared = self.prepare()
            self.assertEqual(prepared['routing']['worker_qualification']['snapshot']['sha256'], qmod.digest(selected))
            self.assertEqual(qmod.load(pointer)['artifact'], selected)
            self.assertEqual(ao.read(config)['family_qualification'], pointer)
        self.assertNotIn('family_qualification', ao.read(config))


class AncestryFixture(ModelFixture):
    """An exact claude-fable-5-1 room: the exact-epoch regressions stay exact (family rooms: test_ao_engineering_family)."""

    def setUp(self):
        super().setUp()
        self.room = self.open_model('ancestry', model=FABLE)['room_id']
        self.spec()
        self.bind_at(FABLE)
        self.agree()

    def switch(self, **kwargs):
        """Publish a committed fable -> opus transition and move live AO settings to opus."""
        parts = self.parts(**kwargs)
        self.publish(parts)
        self.fake.snapshots['engineer']['settings']['model'] = OPUS
        return parts


class AncestryTests(AncestryFixture):
    """V2-3/V2-5/V2-8: committed ancestry keeps every original record byte-identical."""

    def test_committed_transition_preserves_every_prior_record_and_moves_identity(self):
        directory = self.directory()
        before_files, before_bindings = files_under(directory), copy.deepcopy(self.state()['bindings'])
        before_requests = copy.deepcopy(self.state()['requests'])
        parts = self.switch()
        state = self.state()
        self.assertEqual(files_under(directory), before_files)
        self.assertEqual(state['bindings'], before_bindings)
        self.assertEqual(state['requests'], before_requests)
        self.assertEqual(state['bindings']['engineer']['model'], FABLE)
        history = em.epochs(directory, state)
        self.assertEqual([(e['model'], e['from_order']) for e in history], [(FABLE, 1), (OPUS, 2)])
        self.assertEqual(history[-1]['qualification']['minimum_claude_code_version'], '2.1.280')
        self.assertEqual(history[-1]['record_sha256'], parts['record_sha256'])
        self.assertEqual(em.effective_binding(directory, state)['model'], OPUS)
        self.assertEqual(em.current_qualification(directory, state), em.BUNDLED_MODELS[OPUS])
        # Historical requests keep the epoch that owned them; live settings follow the new epoch.
        self.assertEqual(em.epoch_for_order(history, 1)['model'], FABLE)
        self.assertEqual(em.epoch_for_order(history, 2)['model'], OPUS)
        self.service.ao_room_sync(self.room)
        self.assertTrue(ao_workflow.agreement(self.service, directory, self.state())['agreed'])
        self.assertEqual(ao_workflow.agreement(self.service, directory, self.state())['request_id'], 'spec_review')
        self.assertEqual(files_under(directory), before_files)

    def test_status_separates_configured_initial_and_observed_identity(self):
        self.switch()
        status = self.service.ao_room_status(self.room)
        summary = status['engineering_model']
        self.assertEqual(summary['configured']['model'], OPUS)
        self.assertEqual(summary['initial']['model'], FABLE)
        self.assertEqual(summary['initial']['selection'], 'explicit')
        self.assertEqual(status['bindings']['engineer']['model'], FABLE)  # the stored binding never changes
        self.assertEqual(summary['observed']['settings'], {'model': OPUS, 'reasoningEffort': 'max'})
        self.assertEqual(summary['observed']['basis'], 'AO stored settings for the next turn; not native adoption')
        self.assertEqual(summary['observed']['request_id'], 'switch-model')
        self.assertEqual(summary['resolution']['status'], 'exact')
        self.assertEqual(summary['resolution']['expected_model'], OPUS)
        self.assertEqual(summary['effort'], {'configured': 'max', 'effective': 'not evidenced'})
        self.assertEqual(summary['attempts'], [{'request_id': 'switch-model', 'state': 'committed',
                                                'outcome': 'committed',
                                                'source': {'configured_model': FABLE, 'reasoning_effort': 'max'},
                                                'target': {'configured_model': OPUS, 'reasoning_effort': 'max'},
                                                'record_sha256': summary['epochs'][-1]['record_sha256'],
                                                'boundary_order': 1}])
        self.assertIsNone(summary['pending'])
        self.assertEqual(summary['basis'], 'Configured AO settings and recorded transitions; '
                                           'not provider availability or native attestation')
        for claim in ('"available"', '"account"', '"quota"', 'adopted', 'attested'):
            self.assertNotIn(claim, json.dumps(summary))

    def test_live_settings_still_on_the_old_model_refuse_identity(self):
        self.switch()
        self.fake.snapshots['engineer']['settings']['model'] = FABLE
        with self.assertRaisesRegex(ao.RoomError, 'AO configured model/effort changed or is unavailable'):
            self.service.ao_room_sync(self.room)

    def test_pre_boundary_request_claiming_the_new_model_is_refused(self):
        self.switch()
        state = self.state()
        state['requests']['spec_review']['model'] = OPUS
        ao.atomic(self.directory() / 'state.json', state)
        with self.assertRaisesRegex(ao.RoomError, 'contradicts its recorded engineering epoch'):
            em.check_request(self.directory(), self.state(), self.state()['requests']['spec_review'])
        with self.assertRaisesRegex(ao.RoomError, 'contradicts its recorded engineering epoch'):
            ao_workflow.agreement(self.service, self.directory(), self.state())
        with self.assertRaisesRegex(ao.RoomError, 'contradicts its recorded engineering epoch'):
            self.service.ao_room_sync(self.room)

    def test_new_request_after_the_boundary_records_the_new_model(self):
        self.switch()
        self.assertTrue(ao_workflow.agreement(self.service, self.directory(), self.state())['agreed'])
        self.send('spec_review', 'review-two')
        request = self.state()['requests']['review-two']
        self.assertEqual((request['model'], request['created_order']), (OPUS, 2))
        self.assertEqual(request['engineering_resolution'],
                         {'selector': {'kind': 'exact', 'model': OPUS}, 'configured_model': OPUS,
                          'expected_model': OPUS, 'resolution_sha256': None,
                          'effort_basis': 'configured max intent; effective effort not evidenced'})
        self.assertEqual(em.check_request(self.directory(), self.state(), request)['model'], OPUS)
        self.assertEqual(self.state()['requests']['spec_review']['engineering_resolution']['expected_model'], FABLE)
        self.assertEqual(self.state()['bindings']['engineer']['model'], FABLE)

    def test_stored_binding_contradicting_epoch_zero_is_refused(self):
        directory = self.directory()
        tampered = copy.deepcopy(self.state())
        tampered['bindings']['engineer']['model'] = OPUS
        with self.assertRaisesRegex(ao.RoomError, "contradicts this room's initial engineering model"):
            em.effective_binding(directory, tampered)
        # After a committed transition the frozen audit evidence pins the same binding,
        # so the tamper is refused by the journal before any epoch is derived.
        self.switch()
        state = self.state()
        state['bindings']['engineer']['model'] = OPUS
        ao.atomic(directory / 'state.json', state)
        with self.assertRaisesRegex(ao.RoomError, 'Unclaimed, missing or inconsistent'):
            em.effective_binding(directory, self.state())

    def test_binding_for_request_and_expected_settings_follow_the_epoch(self):
        self.switch()
        directory, state = self.directory(), self.state()
        frozen = copy.deepcopy(state['bindings']['engineer'])
        historical = state['requests']['spec_review']
        self.assertEqual(em.binding_for_request(directory, state, frozen, historical)['model'], FABLE)
        self.assertEqual(em.expected_settings(directory, state, historical),
                         {'model': OPUS, 'reasoning_effort': 'max'})
        self.assertEqual(em.expected_settings(directory, state, frozen), {'model': OPUS, 'reasoning_effort': 'max'})
        self.assertEqual(em.expected_settings(directory, state, state['bindings']['reviewer']),
                         {'model': 'astra', 'reasoning_effort': 'max'})
        stale = {**frozen, 'model': 'claude-other-1-1'}
        with self.assertRaisesRegex(ao.RoomError, 'contradicts this room'):
            em.expected_settings(directory, state, stale)


class EvidenceTests(AncestryFixture):
    """Review finding 2: a hash-consistent record is not proof of an external mutation."""

    def test_minimal_hash_consistent_committed_pair_cannot_manufacture_an_epoch(self):
        """The literal review counterexample: matching digests, no audited proof at all."""
        directory, state = self.directory(), self.state()
        intent = {'version': 1, 'room_id': state['room_id'], 'request_id': 'minimal',
                  'source': {'model': FABLE, 'reasoning_effort': 'max'},
                  'target': {'model': OPUS, 'reasoning_effort': 'max'}}
        intent_sha = ao.digest(intent)
        record = {**intent, 'outcome': 'committed', 'intent_sha256': intent_sha, 'boundary_order': 0}
        em.store_once(directory, '%s/requests/minimal.json' % em.BASE, intent)
        em.store_once(directory, '%s/records/minimal.json' % em.BASE, record)
        proposed = {**state, em.POINTER_KEY: {
            'request_id': 'minimal', 'intent': '%s/requests/minimal.json' % em.BASE,
            'intent_sha256': intent_sha, 'state': 'committed',
            'record': '%s/records/minimal.json' % em.BASE, 'record_sha256': ao.digest(record)}}
        for call in (em.journal, em.epochs, em.current, em.effective_binding):
            with self.subTest(call=call.__name__):
                with self.assertRaisesRegex(ao.RoomError, 'Unclaimed, missing or inconsistent'):
                    call(directory, proposed)
        self.clear_journal()

    def test_committed_record_without_an_attempt_marker_is_refused(self):
        self.assert_refused(self.parts(drop_marker=True))
        # ... and a marker file without its record is not a commit either.
        self.assert_refused(self.parts(record_changes={'attempt_sha256': None}))

    def test_every_mandatory_evidence_key_is_required(self):
        for key in sorted(em.EVIDENCE_FIELDS):
            with self.subTest(key=key):
                self.assert_refused(self.parts(evidence_drop=key))

    def test_evidence_cross_links_must_bind_the_audited_room(self):
        engineer = self.state()['bindings']['engineer']
        native = self.parts()['intent']['evidence']['native']
        target = self.parts()['intent']['evidence']['target']
        cases = [{'room_id': 'ao-other'}, {'version': 2}, {'state_sha256': 'f' * 64},
                 {'source': {'selector': selector(OPUS), 'configured_model': OPUS, 'reasoning_effort': 'max'}},
                 {'target': {**target, 'policy_sha256': 'f' * 64}},
                 {'target': {**target, 'qualification': dict(ENTRY)}},
                 {'target': {**target, 'selector': selector('opus'), 'configured_model': 'opus'}},
                 {'engineer': {**engineer, 'session_id': 'someone-else'}},
                 {'spec_record_sha256': 'f' * 64}, {'candidate': {'sha256': 'f' * 64}},
                 {'candidate': 'not-a-dict'}, {'native_owner': 'not-a-dict'}, {'journals': {}},
                 {'native': {**native, 'controller': 'stopped'}},
                 {'native': {**native, 'session_id': 'someone-else'}},
                 {'native': {**native, 'conversation_id': 'other-conversation'}},
                 {'native': {**native, 'branch_id': 'other-branch'}},
                 {'native': {**native, 'history_sha256': 'f' * 64}},
                 {'native': {**native, 'transcript_sha256': 'short'}},
                 {'native': {**native, 'settings': {'model': OPUS, 'reasoningEffort': 'max'}}},
                 {'native': {**native, 'settings': {'model': FABLE, 'reasoningEffort': 'high'}}}]
        for change in cases:
            with self.subTest(change=sorted(change)):
                self.assert_refused(self.parts(evidence_changes=change))
        # R2: the audited owner is the bound engineer's conversation/branch, the transcript's session, idle.
        for change in ({'id': 'someone-else'}, {'ao_conversation_id': 'other-conversation'},
                       {'active_branch_id': 'other-branch'}, {'provider_conversation_id': 'other-native'},
                       {'provider_conversation_id': ''}, {'activity_state': 'active'},
                       {'activity_state': 'exited'}, {'activity_state': None}):
            with self.subTest(owner=change):
                self.assert_refused(self.parts(owner_changes=change))

    def test_unpinned_target_qualification_or_policy_is_refused(self):
        policy = em.effective_policy(self.service.root)[0]
        for change in ({'qualification': dict(ENTRY)},
                       {'policy': {**policy, 'models': {FABLE: dict(ENTRY)}}},
                       {'policy_sha256': 'f' * 64}, {'harness': 'codex'}, {'reasoning_effort': 'high'},
                       {'configured_model': FABLE}, {'configured_model': 'opus'},
                       {'selector': selector(FABLE), 'configured_model': FABLE},
                       {'selector': selector('claude-unqualified-9-9'), 'configured_model': 'claude-unqualified-9-9'},
                       {'selector': selector('sonnet'), 'configured_model': 'sonnet',
                        'qualification': policy['families']['sonnet']},
                       {'selector': 'opus', 'configured_model': 'opus'}):
            with self.subTest(change=sorted(change)):
                self.assert_refused(self.parts(target_changes=change))
        trimmed = {k: v for k, v in self.parts()['intent']['target'].items() if k != 'policy'}
        self.assert_refused(self.parts(intent_changes={'target': trimmed}))
        legacy = {k: v for k, v in self.parts()['intent']['target'].items() if k not in ('selector', 'configured_model')}
        self.assert_refused(self.parts(intent_changes={'target': {**legacy, 'model': OPUS}}))  # v2 shape

    def test_observed_after_not_equal_to_the_target_is_refused(self):
        after = self.parts()['record']['observed_after']
        for settings in ({'model': FABLE, 'reasoningEffort': 'max'},
                         {'model': OPUS, 'reasoningEffort': 'high'},
                         {'model': OPUS, 'reasoningEffort': 'max', 'approvalMode': 'auto'},
                         {'model': OPUS}):
            with self.subTest(settings=settings):
                self.assert_refused(self.parts(record_changes={'observed_after': {**after, 'settings': settings}}))
        for change in ({'history_sha256': 'f' * 64}, {'controller': 'stopped', 'settings': None}):
            with self.subTest(change=sorted(change)):
                self.assert_refused(self.parts(record_changes={'observed_after': {**after, **change}}))
        before = self.parts()['record']['observed_before']
        self.assert_refused(self.parts(record_changes={'observed_before': {**before, 'history_sha256': 'f' * 64}}))
        self.assert_refused(self.parts(record_changes={'observed_before': {
            **before, 'settings': {'model': FABLE, 'reasoningEffort': 'max', 'approvalMode': 'auto'}}}))

    def test_boundary_must_equal_the_inventory_length(self):
        self.assertEqual(self.parts()['record']['boundary_order'], 1)
        for boundary in (0, 2, -1, True):
            with self.subTest(boundary=boundary):
                self.assert_refused(self.parts(boundary=boundary))

    def test_record_shape_outcome_and_patch_result_are_validated(self):
        for change in ({'outcome': 'not_applied'}, {'outcome': 'abandoned_unchanged'}, {'version': 2},
                       {'room_id': 'ao-other'}, {'request_id': 'other'}, {'intent_sha256': 'f' * 64},
                       {'attempt_sha256': 'f' * 64}, {'recorded_at': 'later'},
                       {'patch_result': {'acknowledged': 'yes', 'error': None, 'response_sha256': None}},
                       {'patch_result': {'acknowledged': True, 'error': None}},
                       {'patch_result': {'acknowledged': True, 'error': 7, 'response_sha256': None}},
                       {'patch_result': {'acknowledged': True, 'error': None, 'response_sha256': 'short'}},
                       {'abandonment': {'authorization': 'a', 'diagnosis': 'd', 'abandon_inputs_sha256': 'e' * 64}}):
            with self.subTest(change=sorted(change)):
                self.assert_refused(self.parts(record_changes=change))

    def test_valid_abandoned_record_adds_no_epoch(self):
        parts = self.parts(request_id='abandon-one', outcome='abandoned_unchanged')
        self.publish(parts, pointer_state='abandoned')
        directory, state = self.directory(), self.state()
        self.assertEqual([e['model'] for e in em.epochs(directory, state)], [FABLE])
        chain = em.journal(directory, state)
        self.assertEqual(chain['committed'], [])
        self.assertEqual(chain['attempts'][0]['outcome'], 'abandoned_unchanged')
        self.assertEqual(em.effective_binding(directory, state)['model'], FABLE)
        summary = self.service.ao_room_status(self.room)['engineering_model']
        self.assertEqual(summary['configured']['model'], FABLE)
        self.assertEqual(summary['attempts'][0]['outcome'], 'abandoned_unchanged')
        self.assertIsNone(summary['observed'])

    def test_abandoned_record_with_changed_observed_settings_is_refused(self):
        after = self.parts(outcome='abandoned_unchanged')['record']['observed_after']
        self.assert_refused(self.parts(outcome='abandoned_unchanged',
                                       record_changes={'observed_after': {**after, 'settings': {'model': OPUS, 'reasoningEffort': 'max'}}}),
                            pointer_state='abandoned')
        self.assert_refused(self.parts(outcome='abandoned_unchanged', drop_marker=True,
                                       record_changes={'attempt_sha256': 'f' * 64}), pointer_state='abandoned')

    def test_abandoned_record_without_a_marker_is_allowed_before_the_patch(self):
        chain, history = self.assert_accepted(self.parts(request_id='pre-attempt', outcome='abandoned_unchanged',
                                                         drop_marker=True), pointer_state='abandoned')
        self.assertIsNone(chain['attempts'][0]['attempt_sha256'])
        self.assertEqual([e['model'] for e in history], [FABLE])

    def test_abandonment_block_is_mandatory_and_typed(self):
        for change in ({'abandonment': None}, {'abandonment': {'authorization': 'x'}},
                       {'abandonment': {'authorization': ' ', 'diagnosis': 'd', 'abandon_inputs_sha256': 'e' * 64}},
                       {'abandonment': {'authorization': 'a', 'diagnosis': 'd', 'abandon_inputs_sha256': 'short'}}):
            with self.subTest(change=sorted(change)):
                self.assert_refused(self.parts(outcome='abandoned_unchanged', record_changes=change),
                                    pointer_state='abandoned')

    def test_patch_payload_must_preserve_a_nonempty_approval_mode(self):
        chain, _ = self.assert_accepted(self.parts(approval='auto'))
        payload = chain['attempts'][0]['intent_value']['patch']['payload']
        self.assertEqual(payload, {'model': OPUS, 'reasoningEffort': 'max', 'approvalMode': 'auto'})
        session = self.state()['bindings']['engineer']['session_id']
        path = '/sessions/%s/conversation/settings' % session
        for approval, forged in (('auto', {'model': OPUS, 'reasoningEffort': 'max'}),
                                 ('auto', {'model': OPUS, 'reasoningEffort': 'max', 'approvalMode': 'manual'}),
                                 (None, {'model': OPUS, 'reasoningEffort': 'max', 'approvalMode': 'auto'}),
                                 ('', {'model': OPUS, 'reasoningEffort': 'max', 'approvalMode': ''})):
            with self.subTest(approval=approval, forged=sorted(forged)):
                self.assert_refused(self.parts(approval=approval,
                                               intent_changes={'patch': {'method': 'PATCH', 'path': path, 'payload': forged}}))

    def test_patch_path_and_method_are_bound_to_the_engineer_session(self):
        session = self.state()['bindings']['engineer']['session_id']
        path = '/sessions/%s/conversation/settings' % session
        payload = {'model': OPUS, 'reasoningEffort': 'max'}
        for call in ({'method': 'POST', 'path': path, 'payload': payload},
                     {'method': 'PATCH', 'path': '/sessions/other/conversation/settings', 'payload': payload},
                     {'method': 'PATCH', 'path': path, 'payload': {'model': FABLE, 'reasoningEffort': 'max'}},
                     {'method': 'PATCH', 'path': path, 'payload': {'model': OPUS, 'reasoningEffort': 'high'}},
                     {'method': 'PATCH', 'path': path, 'payload': {**payload, 'temperature': 1}},
                     {'method': 'PATCH', 'path': path, 'payload': payload, 'headers': {}}):
            with self.subTest(call=sorted(call)):
                self.assert_refused(self.parts(intent_changes={'patch': call}))

    def test_intent_cross_links_and_input_shape_are_validated(self):
        inputs = self.parts()['intent']['inputs']
        for change in ({'inputs_sha256': 'f' * 64}, {'audit_sha256': 'f' * 64},
                       {'provider_transition_receipt_sha256': 'f' * 64},
                       {'before_state_sha256': 'f' * 64}, {'version': 2}, {'recorded_at': 'later'},
                       {'room_id': 'ao-other'}, {'request_id': 'other'}):
            with self.subTest(change=sorted(change)):
                self.assert_refused(self.parts(intent_changes=change))
        for forged in ({**inputs, 'target_model': FABLE}, {**inputs, 'source_model': OPUS},
                       {**inputs, 'request_id': 'other'}, {**inputs, 'audit_sha256': 'f' * 64},
                       {k: v for k, v in inputs.items() if k != 'reason'},
                       {**inputs, 'extra': 'value'}, {**inputs, 'reason': 7}):
            with self.subTest(forged=sorted(forged)):
                self.assert_refused(self.parts(intent_changes={'inputs': forged,
                                                               'inputs_sha256': ao.digest(forged)}))
        trimmed = {k: v for k, v in self.parts()['intent'].items() if k != 'patch'}
        state = {**self.state(), em.POINTER_KEY: {
            'request_id': 'trimmed', 'intent': '%s/requests/trimmed.json' % em.BASE,
            'intent_sha256': ao.digest(trimmed), 'state': 'pending', 'record': None, 'record_sha256': None}}
        em.store_once(self.directory(), '%s/requests/trimmed.json' % em.BASE, trimmed)
        with self.assertRaisesRegex(ao.RoomError, 'Unclaimed, missing or inconsistent'):
            em.journal(self.directory(), state, allow_pending=True)
        self.clear_journal()


class OrderContractTests(AncestryFixture):
    """Review finding 5: attributable, unique, retained creation orders."""

    def test_epoch_for_order_refuses_missing_zero_negative_boolean_and_text(self):
        history = em.epochs(self.directory(), self.state())
        for order in (None, 0, -1, True, False, '3', 2.0):
            with self.subTest(order=order), self.assertRaisesRegex(ao.RoomError, 'creation order'):
                em.epoch_for_order(history, order)
        self.assertEqual(em.epoch_for_order(history, 1)['model'], FABLE)

    def test_check_request_refuses_malformed_or_unretained_requests(self):
        directory, state = self.directory(), self.state()
        request = state['requests']['spec_review']
        self.assertEqual(em.check_request(directory, state, request)['model'], FABLE)
        for order in (None, 0, -1, True, '1'):
            with self.subTest(order=order):
                broken = copy.deepcopy(state)
                broken['requests']['spec_review']['created_order'] = order
                with self.assertRaisesRegex(ao.RoomError, 'creation order'):
                    em.check_request(directory, broken, broken['requests']['spec_review'])
        detached = copy.deepcopy(request)
        detached['text'] = 'a different message'
        with self.assertRaisesRegex(ao.RoomError, 'not this room'):
            em.check_request(directory, state, detached)
        missing = {**copy.deepcopy(request), 'request_id': 'never-retained'}
        with self.assertRaisesRegex(ao.RoomError, 'not this room'):
            em.check_request(directory, state, missing)

    def test_committed_transition_requires_contiguous_unique_orders(self):
        self.switch()
        self.send('spec_review', 'review-two')
        directory = self.directory()
        self.assertEqual([e['model'] for e in em.epochs(directory, self.state())], [FABLE, OPUS])
        for change in ({'spec_review': 1, 'review-two': 1}, {'spec_review': 1, 'review-two': 3},
                       {'spec_review': 0, 'review-two': 2}, {'spec_review': 1, 'review-two': True}):
            with self.subTest(change=change):
                state = self.state()
                for name, order in change.items():
                    state['requests'][name]['created_order'] = order
                with self.assertRaisesRegex(ao.RoomError, 'creation order'):
                    em.epochs(directory, state)

    def test_inventory_must_match_the_retained_pre_boundary_requests(self):
        self.assertEqual(self.parts()['intent']['evidence']['requests'],
                         [{'request_id': 'spec_review', 'created_order': 1}])
        # Internally consistent, but it is not this room's retained pre-boundary inventory.
        forged = [{'request_id': 'not-a-request', 'created_order': 1}]
        self.assert_refused(self.parts(evidence_changes={'requests': forged}), journal=False)

    def test_inventory_orders_must_be_exactly_one_to_n(self):
        for forged in ([{'request_id': 'spec_review', 'created_order': 2}],
                       [{'request_id': 'spec_review', 'created_order': 0}],
                       [{'request_id': 'spec_review', 'created_order': True}],
                       [{'request_id': 'spec_review', 'created_order': 1},
                        {'request_id': 'spec_review', 'created_order': 2}],
                       [{'request_id': 'spec_review'}], 'not-a-list'):
            with self.subTest(forged=forged):
                self.assert_refused(self.parts(evidence_changes={'requests': forged}))

    def test_malformed_retained_shapes_are_the_ordinary_order_refusal(self):
        self.switch()
        directory, state = self.directory(), self.state()
        self.assertEqual([r['request_id'] for r in em._orders(state)], ['spec_review'])
        for damaged in ('truthy-non-mapping', 7, ['spec_review'], []):
            with self.subTest(requests=damaged):
                broken = {**copy.deepcopy(state), 'requests': damaged}
                with self.assertRaisesRegex(ao.RoomError, 'creation order'):
                    em._orders(broken)
                with self.assertRaisesRegex(ao.RoomError, 'creation order'):
                    em.epochs(directory, broken)
        for damaged in ('truthy-non-mapping', 7, ['spec_review']):
            with self.subTest(request=damaged):
                broken = copy.deepcopy(state)
                broken['requests']['spec_review'] = damaged
                with self.assertRaisesRegex(ao.RoomError, 'creation order'):
                    em.epochs(directory, broken)
        broken = copy.deepcopy(state)
        broken['requests']['spec_review'] = 'truthy-non-mapping'
        self.assertEqual(em.summary(directory, broken), {'error': em.ORDERS, 'basis': em.BASIS})


class PendingJournalTests(AncestryFixture):
    """V2-3: a pending attempt blocks every ordinary change while status still explains it."""

    def test_pending_intent_blocks_mutations_but_status_reports_it(self):
        parts = self.parts(request_id='pending-one', drop_marker=True)
        pointer = self.publish(parts, pointer_state='pending', record=False, marker=False)
        directory = self.directory()
        with self.assertRaisesRegex(ao.RoomError, 'uncommitted engineering model transition exists'):
            em.journal(directory, self.state())
        with self.assertRaisesRegex(ao.RoomError, 'uncommitted engineering model transition exists'):
            self.service.settled(self.state())
        with self.assertRaisesRegex(ao.RoomError, 'uncommitted engineering model transition exists'):
            self.service.ao_room_sync(self.room)
        with self.assertRaisesRegex(ao.RoomError, 'uncommitted engineering model transition exists'):
            self.send('spec_review', 'blocked')
        self.assertNotIn('blocked', self.state()['requests'])
        status = self.service.ao_room_status(self.room)['engineering_model']
        self.assertEqual(status['pending'], {k: pointer[k] for k in ('request_id', 'intent', 'intent_sha256')})
        self.assertEqual(status['configured']['model'], FABLE)
        chain = em.journal(directory, self.state(), allow_pending=True)
        self.assertEqual(chain['pending']['request_id'], 'pending-one')
        self.assertIsNone(chain['pending']['record_value'])
        self.assertIsNone(chain['pending']['attempt_value'])
        self.assertEqual(chain['committed'], [])
        # A pending transition may still be allowed through explicitly, for reconciliation only.
        self.service.settled(self.state(), pending_model_transition=True)

    def test_pending_attempt_marker_and_crashed_record_are_exposed_not_discarded(self):
        parts = self.parts(request_id='pending-two')
        self.publish(parts, pointer_state='pending', record=False)
        chain = em.journal(self.directory(), self.state(), allow_pending=True)
        self.assertEqual(chain['pending']['attempt_sha256'], ao.digest(parts['marker']))
        self.assertIsNone(chain['pending']['record_value'])
        # Crash after the record write, before the pointer commit.
        em.store_once(self.directory(), '%s/records/pending-two.json' % em.BASE, parts['record'])
        chain = em.journal(self.directory(), self.state(), allow_pending=True)
        self.assertEqual(chain['pending']['record_value'], parts['record'])
        self.assertEqual(chain['committed'], [])  # still no epoch until the pointer commits
        self.assertEqual([e['model'] for e in em.epochs(self.directory(), self.state())], [FABLE])
        with self.assertRaisesRegex(ao.RoomError, 'uncommitted engineering model transition exists'):
            em.journal(self.directory(), self.state())

    def test_unclaimed_intent_without_a_pointer_is_refused(self):
        parts = self.parts(request_id='orphan')
        self.publish(parts, record=False, marker=False, save=False)
        with self.assertRaisesRegex(ao.RoomError, 'Unclaimed, missing or inconsistent'):
            em.journal(self.directory(), self.state())
        with self.assertRaisesRegex(ao.RoomError, 'Unclaimed, missing or inconsistent'):
            self.service.ao_room_sync(self.room)
        self.assertIn('Unclaimed', self.service.ao_room_status(self.room)['engineering_model']['error'])

    def test_modified_record_or_intent_bytes_are_refused(self):
        parts = self.switch()
        for relative in ('%s/records/switch-model.json' % em.BASE, '%s/requests/switch-model.json' % em.BASE,
                         '%s/attempts/switch-model.json' % em.BASE):
            with self.subTest(relative=relative):
                path = self.directory() / relative
                original = path.read_bytes()
                path.write_bytes(original.replace(b'"version": 1', b'"version": 1 '))
                self.assertNotEqual(path.read_bytes(), original)
                path.write_bytes(json.dumps({**json.loads(original), 'recorded_at': 9.0}).encode() + b'\n')
                with self.assertRaisesRegex(ao.RoomError, 'Unclaimed, missing or inconsistent'):
                    em.journal(self.directory(), self.state())
                path.write_bytes(original)
                em.journal(self.directory(), self.state())
        self.assertEqual(em.current(self.directory(), self.state())['record_sha256'], parts['record_sha256'])

    def test_pointer_shape_and_chain_bounds_are_validated(self):
        parts = self.switch()
        pointer = self.state()[em.POINTER_KEY]
        for change in ({'state': 'not_applied'}, {'state': 'committed', 'record': None},
                       {'record_sha256': 'short'}, {'intent': 'elsewhere/x.json'},
                       {'request_id': 'other'}, {'extra': 1}):
            with self.subTest(change=change):
                state = self.state()
                state[em.POINTER_KEY] = {**pointer, **change}
                with self.assertRaisesRegex(ao.RoomError, 'Unclaimed, missing or inconsistent'):
                    em.journal(self.directory(), state)
        # A pending pointer whose record already exists is the crash window, not damage:
        # it is validated and exposed for reconciliation, and still refused without allow_pending.
        state = self.state()
        state[em.POINTER_KEY] = {**pointer, 'state': 'pending', 'record': None, 'record_sha256': None}
        chain = em.journal(self.directory(), state, allow_pending=True)
        self.assertEqual(chain['pending']['record_value'], parts['record'])
        self.assertEqual(chain['committed'], [])
        self.assertEqual([e['model'] for e in em.epochs(self.directory(), state)], [FABLE])
        with self.assertRaisesRegex(ao.RoomError, 'uncommitted engineering model transition exists'):
            em.journal(self.directory(), state)
        self.assertEqual(parts['record']['outcome'], 'committed')

    def test_a_second_transition_chains_from_the_first_terminal_pointer(self):
        first = self.switch()
        pointer = self.state()[em.POINTER_KEY]
        second = self.parts(request_id='switch-back', source_model=OPUS, target_model=FABLE, previous=pointer)
        self.fake.snapshots['engineer']['settings']['model'] = OPUS
        self.publish(second)
        directory, state = self.directory(), self.state()
        self.assertEqual([e['model'] for e in em.epochs(directory, state)], [FABLE, OPUS, FABLE])
        self.assertEqual(len(em.journal(directory, state)['committed']), 2)
        self.assertEqual(em.current(directory, state)['model'], FABLE)
        self.assertEqual(first['intent']['previous'], None)
        orphan = self.parts(request_id='unchained', source_model=OPUS, target_model=FABLE, previous=None)
        self.publish(orphan, save=False)
        with self.assertRaisesRegex(ao.RoomError, 'Unclaimed, missing or inconsistent'):
            em.journal(directory, self.state())


class DurabilityTests(AncestryFixture):
    """Review findings 3 and 4: durable publication and symlink-safe absence."""

    def test_identical_retry_resynchronizes_the_parent_directory(self):
        directory, relative = self.directory(), '%s/requests/probe.json' % em.BASE
        em.store_once(directory, '%s/requests/seed.json' % em.BASE, {'seed': True})  # ancestors now exist
        real, remaining = os.fsync, [1]

        def failing(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode) and remaining:
                remaining.pop()
                raise OSError('simulated directory fsync failure')
            return real(fd)

        with patch.object(os, 'fsync', failing), self.assertRaises(OSError):
            em.store_once(directory, relative, {'probe': 1})
        self.assertEqual((directory / relative).read_bytes(), em.json_bytes({'probe': 1}))
        observed = []

        def recording(fd):
            observed.append('dir' if stat.S_ISDIR(os.fstat(fd).st_mode) else 'file')
            return real(fd)

        with patch.object(os, 'fsync', recording):
            digest = em.store_once(directory, relative, {'probe': 1})
        self.assertEqual(digest, ao.digest({'probe': 1}))
        # R1: the identical retry republishes every ancestor entry, parent first, up to the room directory.
        self.assertEqual(observed, ['file', 'dir', 'dir', 'dir'])
        with self.assertRaisesRegex(ao.RoomError, 'already exists with other bytes'):
            em.store_once(directory, relative, {'probe': 2})

    def test_created_ancestors_are_published_one_level_at_a_time(self):
        directory = self.directory()
        observed = []
        real = os.fsync

        def recording(fd):
            observed.append('dir' if stat.S_ISDIR(os.fstat(fd).st_mode) else 'file')
            return real(fd)

        with patch.object(os, 'fsync', recording):
            em.store_once(directory, '%s/records/deep.json' % em.BASE, {'deep': True})
        # Each created directory is published into its parent, then every ancestor is republished.
        self.assertEqual(observed, ['dir', 'dir', 'file', 'dir', 'dir', 'dir'])
        self.assertEqual(stat.S_IMODE((directory / em.BASE).lstat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((directory / em.BASE / 'records' / 'deep.json').lstat().st_mode), 0o600)

    def test_symlinked_journal_root_or_subdirectory_is_never_absence(self):
        directory, elsewhere = self.directory(), self.root / 'elsewhere'
        elsewhere.mkdir()
        (directory / em.BASE).symlink_to(elsewhere)
        state = self.state()
        self.assertIsNone(state.get(em.POINTER_KEY))
        with self.assertRaisesRegex(ao.RoomError, 'Unclaimed, missing or inconsistent'):
            em.journal(directory, state)
        with self.assertRaisesRegex(ao.RoomError, 'real owned room directories'):
            em.store_once(directory, '%s/requests/x.json' % em.BASE, {'x': 1})
        (directory / em.BASE).unlink()
        (directory / em.BASE).mkdir(mode=0o700)
        (directory / em.BASE / 'requests').symlink_to(elsewhere)
        with self.assertRaisesRegex(ao.RoomError, 'Unclaimed, missing or inconsistent'):
            em.journal(directory, self.state())
        with self.assertRaisesRegex(ao.RoomError, 'real owned room directories'):
            em.store_once(directory, '%s/requests/x.json' % em.BASE, {'x': 1})
        self.assertFalse((elsewhere / 'x.json').exists())

    def test_symlinked_or_stray_journal_entries_are_refused(self):
        directory = self.directory()
        self.switch()
        strays = directory / em.BASE / 'records'
        (strays / 'notes.txt').write_text('stray')
        with self.assertRaisesRegex(ao.RoomError, 'Unclaimed, missing or inconsistent'):
            em.journal(directory, self.state())
        (strays / 'notes.txt').unlink()
        em.journal(directory, self.state())
        (strays / 'linked.json').symlink_to(strays / 'switch-model.json')
        with self.assertRaisesRegex(ao.RoomError, 'Unclaimed, missing or inconsistent'):
            em.journal(directory, self.state())
        (strays / 'linked.json').unlink()
        (directory / em.BASE / 'stray-directory').mkdir()
        em.journal(directory, self.state())  # an unrelated sibling directory is not a claimed file

    def test_store_once_refuses_paths_outside_the_room(self):
        directory = self.directory()
        for relative in ('/etc/passwd', '../escape.json', 'a/../../escape.json', ''):
            with self.subTest(relative=relative), self.assertRaisesRegex(ao.RoomError, 'room-relative paths only'):
                em.store_once(directory, relative, {'x': 1})


class ProviderEpochTests(AncestryFixture):
    """The provider dispatch gate compares its frozen native identity with this ancestry."""

    def test_model_at_provider_epoch_follows_the_receipt_cross_link(self):
        directory = self.directory()
        self.assertEqual(em.model_at_provider_epoch(directory, self.state())['model'], FABLE)
        # A model transition recorded BEFORE any provider transition carries a null receipt.
        self.switch()
        state = self.state()
        self.assertEqual(em.model_at_provider_epoch(directory, state)['model'], OPUS)
        self.assertEqual(em.current(directory, state)['model'], OPUS)
        self.assertIsNone(em.journal(directory, state)['committed'][0]
                          ['intent_value']['provider_transition_receipt_sha256'])

    def test_a_transition_after_a_committed_provider_epoch_keeps_the_older_epoch_pinned(self):
        state = self.state()
        state['provider_transition'] = {'request_id': 'switch-once', 'receipt_sha256': 'a' * 64, 'state': 'committed'}
        ao.atomic(self.directory() / 'state.json', state)
        parts = self.parts()
        self.assertEqual(parts['intent']['provider_transition_receipt_sha256'], 'a' * 64)
        self.publish(parts)
        directory, state = self.directory(), self.state()
        self.assertEqual(em.current(directory, state)['model'], OPUS)
        # The provider epoch was committed while Fable was still in force.
        self.assertEqual(em.model_at_provider_epoch(directory, state)['model'], FABLE)
        forged = self.parts(request_id='forged', intent_changes={'provider_transition_receipt_sha256': None})
        self.publish(forged, save=False)
        with self.assertRaisesRegex(ao.RoomError, 'Unclaimed, missing or inconsistent'):
            em.journal(directory, self.state())


class ExecutableIntegrationTests(ModelFixture):
    """The executable lanes admit the epoch in force and enforce its qualified minimum."""

    def setUp(self):
        super().setUp()
        self.mid = self.root / 'mid-claude'
        self.new = self.root / 'new-claude'
        for path, version in ((self.mid, '2.1.270'), (self.new, '2.1.280')):
            path.write_text('#!/bin/sh\nprintf "' + version + ' (Claude Code)\\n"\n')
            path.chmod(0o700)
        self.room = self.open_model('executable', model=FABLE)['room_id']
        self.spec()
        self.old = self.install_claude('old-claude', '2.1.268 (Claude Code)')
        self.fake.snapshots['engineer']['settings']['model'] = FABLE
        # A deliberately old executable cannot carry the modern source-qualified worker
        # preparation: the recorded historical v2 bytes keep this lane's own source evidence,
        # and the room's root selection and pinned policy record are never rewritten.
        self.historical_preparation()
        self.service.ao_room_bind(self.room, 'engineer', 'engineer', FABLE, 'max')
        self.service.ao_room_bind(self.room, 'reviewer', 'reviewer', 'astra', 'max')
        self.prepared = ao_delegates.preparation(self.directory(), self.state())
        engineer = self.state()['bindings']['engineer']
        self.owner = {'id': 'engineer', 'project_id': 'project', 'workspace_path': str(self.repo),
                      'ao_conversation_id': engineer['conversation_id'], 'active_branch_id': engineer['branch_id'],
                      'provider_conversation_id': 'native-uuid', 'controller_generation': 'generation-1',
                      'activity_state': 'exited', 'strategy': 'native'}
        p = patch.object(binding, 'read_owner', side_effect=lambda *a: copy.deepcopy(self.owner))
        p.start(); self.addCleanup(p.stop)
        self.fake.sessions['engineer']['isTerminated'] = False
        self.fake.snapshots['engineer'].update(controller='stopped', hasMoreBefore=False, activities=[],
                                               branchMaterialization={'strategy': 'native', 'replayTruncated': False})
        original = self.fake.request

        def request(method, path, payload=None):
            if method == 'GET' and '/conversation?' in path:
                return copy.deepcopy(self.fake.snapshots['engineer'])
            return original(method, path, payload)

        self.fake.request = request
        self.launch = self.root / 'launch-claude'
        self.args = dict(room_id=self.room, request_id='upgrade-1', executable_path=str(self.new),
                         launch_path=str(self.launch), database_path=str(self.root / 'fake.db'),
                         authorization='User authorized this qualified Claude Code upgrade',
                         diagnosis='Claude Code 2.1.268 rejects the newly qualified model before inference')

    def link(self, target):
        if self.launch.is_symlink() or self.launch.exists():
            self.launch.unlink()
        self.launch.symlink_to(target)

    def upgrade(self, **changes):
        defaults = dict(upgrade_authorization='User authorized the newly qualified Claude Code',
                        expected_version='2.1.280')
        return binding.bind(self.service, **{**self.args, **defaults, **changes})

    def commit_switch(self):
        parts = self.parts(request_id='to-opus')
        self.publish(parts)
        self.fake.snapshots['engineer']['settings']['model'] = OPUS
        return parts

    def bindings_exist(self):
        return (self.directory() / 'executable-bindings').exists()

    def test_committed_transition_admits_the_effective_engineer_in_both_lanes(self):
        self.commit_switch()
        self.assertEqual(em.current(self.directory(), self.state())['model'], OPUS)
        self.link(self.new)
        result = self.upgrade()
        self.assertEqual(result['lane'], 'upgrade')
        self.assertEqual(binding.effective(self.directory(), self.state(), self.prepared)['version'],
                         '2.1.280 (Claude Code)')

    def test_replacement_below_the_epoch_minimum_is_refused_before_any_write(self):
        self.commit_switch()
        self.link(self.mid)
        with self.assertRaisesRegex(ao.RoomError, 'claude-opus-5-5 requires Claude Code 2.1.280 or newer'):
            self.upgrade(executable_path=str(self.mid), expected_version='2.1.270')
        self.assertFalse(self.bindings_exist())
        self.old.unlink()  # the diagnosed repair lane is bound by the same minimum
        with self.assertRaisesRegex(ao.RoomError, 'claude-opus-5-5 requires Claude Code 2.1.280 or newer'):
            binding.bind(self.service, **{**self.args, 'request_id': 'repair-1', 'executable_path': str(self.mid)})
        self.assertFalse(self.bindings_exist())
        self.link(self.new)
        self.assertEqual(binding.bind(self.service, **{**self.args, 'request_id': 'repair-2'})['lane'], 'repair')

    def test_rewritten_stored_binding_is_refused_instead_of_accepted(self):
        self.commit_switch()
        self.link(self.new)
        state = self.state()
        state['bindings']['engineer']['model'] = OPUS  # a rewritten receipt, not an ancestry
        ao.atomic(self.directory() / 'state.json', state)
        with self.assertRaisesRegex(ao.RoomError, 'Unclaimed, missing or inconsistent'):
            self.upgrade()
        self.assertFalse(self.bindings_exist())

    def test_pending_model_transition_blocks_the_executable_bind_before_any_write(self):
        parts = self.parts(request_id='pending-switch', drop_marker=True)
        self.publish(parts, pointer_state='pending', record=False, marker=False)
        self.link(self.new)
        before = (self.directory() / 'state.json').read_bytes()
        with self.assertRaisesRegex(ao.RoomError, 'uncommitted engineering model transition exists'):
            self.upgrade()
        self.assertFalse(self.bindings_exist())
        self.assertEqual((self.directory() / 'state.json').read_bytes(), before)

    def test_legacy_room_without_a_minimum_still_upgrades(self):
        self.link(self.new)
        self.assertEqual(self.upgrade()['lane'], 'upgrade')
        self.assertIsNone(em.current_qualification(self.directory(), self.state())
                          .get('minimum_claude_code_version'))


if __name__ == '__main__':
    unittest.main()
