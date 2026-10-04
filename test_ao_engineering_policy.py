"""Standing engineering-model policy: record, revoke and the send-boundary application.

Synthetic offline fixtures only: the fake AO transport, the synthetic native owner/transcript and the
private qualification artifacts live inside the test's temporary directory. No network call is made,
no model is invoked and no PATCH reaches a real endpoint (the fake's request handler is wrapped to
apply or lose the settings PATCH deterministically).
"""
import copy
import io
import json
from contextlib import redirect_stdout
import unittest
from unittest.mock import patch

import ao_engineering_model as em
import ao_engineering_policy as ep
import ao_engineering_transition as et
import ao_project_room as ao
import project_room
import project_room_mcp
from test_ao_engineering_model import FABLE, OPUS
from test_ao_model_qualification import QualificationFixture

NEWER = 'claude-fable-5-2'
OLDER = 'claude-fable-5-0'
AUTH = 'The user recorded the standing follow-newest-qualified decision for this room'


class StandingPolicyFixture(QualificationFixture):
    """An exact-model qualified room plus helpers for the standing-policy surface."""

    def fable_artifact(self, expected=FABLE, revision=3):
        """One synthetic private artifact mapping the engineer family to ``expected``."""
        return self.artifact(families={
            'fable': {'expected_model': expected, 'source_ids': ['opus-family-doc']},
            'opus': {'expected_model': OPUS, 'source_ids': ['opus-family-doc'],
                     'minimum_claude_code_version': '2.1.280'},
            'sonnet': {'expected_model': self.WORKER_SONNET, 'source_ids': ['opus-family-doc']}},
            revision=revision)

    def standing_room(self, model=FABLE, feature='policy-room'):
        """Open an exact-selector room qualified on the fable artifact and complete one review turn."""
        self.qualified_room(feature, artifact=self.fable_artifact(), model=model)
        served = model if em.family_member('fable', model) else FABLE
        self.turn('spec_review', 'spec_review', self.verdict(), served)

    def set_policy(self, family=None):
        return self.service.ao_room_engineer_model_policy(self.room, 'set', AUTH, family)

    def revoke_policy(self, authorization='The user withdrew the standing follow-newest decision'):
        return self.service.ao_room_engineer_model_policy(self.room, 'revoke', authorization)

    def configure_target(self, expected, revision=9):
        """Point the private qualification at ``expected`` and register it as a qualified model."""
        self.configure(self.fable_artifact(expected=expected, revision=revision))
        if expected not in em.BUNDLED_MODELS:
            config = ao.read(self.home / 'ao' / 'config.json')
            models = dict(config.get('engineering_models') or {})
            models[expected] = {'harness': 'claude-code', 'reasoning_effort': 'max',
                                'minimum_claude_code_version': '2.1.280'}
            config['engineering_models'] = models
            ao.atomic(self.home / 'ao' / 'config.json', config)

    def ready(self):
        """The live idle controller the audit freezes its observation on."""
        self.fake.snapshots['engineer']['controller'] = 'ready'

    def patch_settings(self, mode='apply'):
        """Serve the transition's settings PATCH deterministically: apply it, or refuse before applying."""
        original = self.fake.request

        def request(method, path, payload=None):
            if method == 'PATCH' and path.endswith('/conversation/settings'):
                session = path.split('/')[2]
                if mode == 'apply':
                    self.fake.snapshots[session]['settings'].update(copy.deepcopy(payload or {}))
                    return {'settings': copy.deepcopy(self.fake.snapshots[session]['settings'])}
                raise ao.RoomError('AO PATCH failed (URLError); inspect AO and reconcile, never blindly retry')
            return original(method, path, payload)

        patcher = patch.object(self.fake, 'request', side_effect=request)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_pending(self):
        """Leave one uncommitted transition in the journal via a PATCH that is never acknowledged."""
        self.configure_target(NEWER)
        self.ready()
        self.patch_settings('refuse')
        audited = et.audit(self.service, self.room, NEWER,
                           self.source['database'], self.source['transcript'])
        self.assertTrue(audited['eligible'], audited.get('reason'))
        result = et.transition(self.service, self.room, request_id='manual-lost-patch',
                               source_model=FABLE, target_model=NEWER,
                               audit_sha256=audited['audit_sha256'],
                               spec_record_sha256=audited['spec_record_sha256'],
                               candidate_sha256=audited['candidate_sha256'],
                               native_history_sha256=audited['native_history_sha256'],
                               native_owner_database=self.source['database'],
                               native_transcript_path=self.source['transcript'],
                               authorization='The operator authorized this exact transition',
                               reason='synthetic lost PATCH for the pending-epoch fixture')
        self.assertTrue(result['pending'], result)

    def last_application(self):
        return self.state()[ep.POINTER_KEY]['last_application']

    def applications(self):
        base = self.directory() / ep.BASE / 'applications'
        return sorted(base.iterdir()) if base.is_dir() else []

    def records(self):
        base = self.directory() / ep.BASE / 'records'
        return sorted(base.iterdir()) if base.is_dir() else []


class RecordTests(StandingPolicyFixture):
    """set/revoke admission, the state pointer and the digest-chained record files."""

    def test_set_updates_the_status_pointer_and_writes_the_record_chain(self):
        self.standing_room()
        result = self.set_policy()
        self.assertEqual(result['action'], 'set')
        self.assertEqual(result['policy'], 'follow_newest_qualified_family_member')
        self.assertTrue(result['active'])
        self.assertEqual(result['family'], 'fable')
        self.assertEqual(result['qualification_sha256'],
                         em.configured_qualification(self.service.root)['sha256'])
        pointer = self.state()[ep.POINTER_KEY]
        self.assertEqual(set(pointer), ep.POINTER_FIELDS)
        self.assertEqual(pointer['version'], 1)
        self.assertTrue(pointer['active'])
        self.assertEqual(pointer['family'], 'fable')
        self.assertEqual(pointer['role'], 'engineer')
        self.assertEqual(pointer['record'], result['record'])
        self.assertEqual(pointer['sha256'], result['record_sha256'])
        self.assertIsNone(pointer['last_application'])
        files = self.records()
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].name.startswith('1-'))
        record = ao.read(files[0])
        self.assertEqual(set(record), ep.RECORD_FIELDS)
        self.assertEqual(record['action'], 'set')
        self.assertEqual(record['policy'], ep.POLICY)
        self.assertEqual(record['family'], 'fable')
        self.assertEqual(record['authorization'], AUTH)
        self.assertIsNone(record['previous_sha256'])
        self.assertEqual(ao.digest(record), pointer['sha256'])
        self.assertEqual(ao.digest(record)[:12], files[0].name.split('-')[1].removesuffix('.json'))
        status = self.service.ao_room_status(self.room)['engineering_model_policy']
        self.assertEqual(status, {'active': True, 'family': 'fable',
                                  'record_sha256': pointer['sha256'], 'last_application': None})

    def test_set_refused_for_a_family_alias_selector(self):
        self.standing_room(model='fable')
        with self.assertRaisesRegex(ao.RoomError, 'exact identifier'):
            self.set_policy()
        self.assertNotIn(ep.POINTER_KEY, self.state())
        self.assertEqual(self.records(), [])

    def test_set_refused_for_a_different_family(self):
        self.standing_room()
        with self.assertRaisesRegex(ao.RoomError, 'never crosses families'):
            self.set_policy(family='opus')
        with self.assertRaisesRegex(ao.RoomError, 'never crosses families'):
            self.set_policy(family='sonnet')
        self.assertNotIn(ep.POINTER_KEY, self.state())

    def test_set_refused_on_a_pending_epoch(self):
        self.standing_room()
        self.make_pending()
        with self.assertRaisesRegex(ao.RoomError, 'uncommitted engineering model transition'):
            self.set_policy()
        self.assertNotIn(ep.POINTER_KEY, self.state())
        self.assertEqual(self.records(), [])

    def test_revoke_appends_the_chained_record_and_deactivates(self):
        self.standing_room()
        first = self.set_policy()
        result = self.revoke_policy()
        self.assertEqual(result['action'], 'revoke')
        self.assertFalse(result['active'])
        self.assertEqual(result['previous_sha256'], first['record_sha256'])
        pointer = self.state()[ep.POINTER_KEY]
        self.assertFalse(pointer['active'])
        self.assertEqual(pointer['record'], result['record'])
        self.assertEqual(pointer['sha256'], result['record_sha256'])
        files = self.records()
        self.assertEqual(len(files), 2)
        self.assertTrue(files[0].name.startswith('1-') and files[1].name.startswith('2-'))
        second = ao.read(files[1])
        self.assertEqual(second['action'], 'revoke')
        self.assertEqual(second['previous_sha256'], first['record_sha256'])
        self.assertEqual(ao.digest(second)[:12], files[1].name.split('-')[1].removesuffix('.json'))
        with self.assertRaisesRegex(ao.RoomError, 'No active standing'):
            self.revoke_policy()
        # A revoked policy is an inactive policy at the send boundary: no application is recorded.
        outcome = self.send('spec_review', 'after-revoke')
        self.assertEqual(outcome['standing_policy']['outcome'], 'inactive')
        self.assertEqual(self.applications(), [])
        self.native_turn('after-revoke', FABLE)
        self.fake.finish('engineer', self.verdict())
        self.service.ao_room_sync(self.room)


class ApplyTests(StandingPolicyFixture):
    """The stopped-boundary decision and its append-only application evidence."""

    def test_apply_inactive_without_a_policy(self):
        self.standing_room()
        result = self.send('spec_review', 'plain-1')
        self.assertEqual(result['standing_policy'], {'outcome': 'inactive', 'reason': None,
                                                     'from': None, 'to': None,
                                                     'request_id': None, 'record_sha256': None})
        self.assertEqual(self.request('plain-1')['standing_policy']['outcome'], 'inactive')
        self.assertEqual(self.applications(), [])
        self.native_turn('plain-1', FABLE)
        self.fake.finish('engineer', self.verdict())
        self.service.ao_room_sync(self.room)

    def test_apply_no_change_when_the_pin_is_already_the_qualified_member(self):
        self.standing_room()
        self.set_policy()
        self.configure_target(FABLE)
        result = self.send('spec_review', 'same-1')
        self.assertEqual(result['standing_policy']['outcome'], 'no_change')
        self.assertIsNone(result['standing_policy']['reason'])
        self.assertEqual(result['standing_policy']['from'], FABLE)
        self.assertEqual(result['standing_policy']['to'], FABLE)
        self.assertEqual(result['state'], 'submitted')
        self.assertEqual(result['configured_model'], FABLE)
        application = ao.read(self.applications()[-1])
        self.assertEqual(application['outcome'], 'no_change')
        self.assertEqual(application['from'], FABLE)
        self.native_turn('same-1', FABLE)
        self.fake.finish('engineer', self.verdict())
        self.service.ao_room_sync(self.room)

    def test_apply_defers_while_an_engineer_request_is_active(self):
        self.standing_room()
        self.set_policy()
        self.configure_target(FABLE)
        self.send('spec_review', 'busy-1')
        before = len(self.applications())
        with self.assertRaisesRegex(ao.RoomError, 'active or uncertain'):
            self.send('spec_review', 'busy-2')
        self.assertEqual(self.last_application()['outcome'], 'deferred')
        self.assertEqual(self.last_application()['reason'], 'engineer_active')
        self.assertEqual(len(self.applications()), before + 1)
        self.native_turn('busy-1', FABLE)
        self.fake.finish('engineer', self.verdict())
        self.service.ao_room_sync(self.room)

    def test_apply_defers_on_a_semantic_hold(self):
        self.standing_room()
        self.set_policy()
        self.fake.snapshots['engineer']['turns'][-1]['error'] = {'type': 'rate_limit', 'httpStatus': 429}
        self.service.ao_room_sync(self.room)
        self.assertEqual(self.request('spec_review')['semantic_status']['kind'], 'quota_limit')
        with self.assertRaisesRegex(ao.RoomError, 'semantic hold'):
            self.send('spec_review', 'held-1')
        self.assertEqual(self.last_application()['outcome'], 'deferred')
        self.assertEqual(self.last_application()['reason'], 'semantic_hold')

    def test_apply_defers_on_a_pending_transition_epoch(self):
        self.standing_room()
        self.set_policy()
        self.make_pending()
        with self.assertRaisesRegex(ao.RoomError, 'uncommitted engineering model transition'):
            self.send('spec_review', 'pending-1')
        self.assertEqual(self.last_application()['outcome'], 'deferred')
        self.assertEqual(self.last_application()['reason'], 'transition_pending')
        self.assertNotIn('pending-1', self.state()['requests'])

    def test_apply_defers_family_mismatch_when_the_family_is_unqualified(self):
        self.standing_room()
        self.set_policy()
        # The configured artifact drops the engineer family entirely: its expected model is absent,
        # which is not an exact member of the recorded family.
        self.configure(self.artifact())
        result = self.send('spec_review', 'mismatch-1')
        self.assertEqual(result['standing_policy']['outcome'], 'deferred')
        self.assertEqual(result['standing_policy']['reason'], 'family_mismatch')
        self.assertEqual(result['standing_policy']['from'], FABLE)
        self.assertIsNone(result['standing_policy']['to'])
        self.assertEqual(result['state'], 'submitted')
        self.assertEqual(result['configured_model'], FABLE)
        self.native_turn('mismatch-1', FABLE)
        self.fake.finish('engineer', self.verdict())
        self.service.ao_room_sync(self.room)

    def test_apply_defers_a_qualified_member_that_is_not_newer(self):
        self.standing_room()
        self.set_policy()
        self.configure_target(OLDER)
        result = self.send('spec_review', 'older-1')
        self.assertEqual(result['standing_policy']['outcome'], 'deferred')
        self.assertEqual(result['standing_policy']['reason'], 'qualified_not_newer')
        self.assertEqual(result['standing_policy']['from'], FABLE)
        self.assertEqual(result['standing_policy']['to'], OLDER)
        self.assertEqual(result['configured_model'], FABLE)
        self.native_turn('older-1', FABLE)
        self.fake.finish('engineer', self.verdict())
        self.service.ao_room_sync(self.room)

    def test_apply_defers_without_a_registered_native_source(self):
        self.standing_room()
        self.set_policy()
        self.configure_target(NEWER)
        state = self.state()
        state.pop('native_outcome_source')
        ao.atomic(self.directory() / 'state.json', state)
        result = self.send('spec_review', 'nosrc-1')
        # The deferral never blocks the send: it continues on the current pinned model.
        self.assertEqual(result['standing_policy']['outcome'], 'deferred')
        self.assertEqual(result['standing_policy']['reason'], 'no_registered_source')
        self.assertEqual(result['configured_model'], FABLE)
        self.assertEqual(result['state'], 'submitted')
        self.assertEqual(self.last_application()['outcome'], 'deferred')
        self.assertEqual(self.last_application()['reason'], 'no_registered_source')
        self.native_turn('nosrc-1', FABLE)
        self.fake.finish('engineer', self.verdict())
        self.service.ao_room_sync(self.room)

    def test_apply_defers_an_ineligible_audit_and_the_send_dispatches_on_the_pin(self):
        self.standing_room()
        self.set_policy()
        self.configure_target(NEWER)
        self.ready()
        self.owner['activity_state'] = 'active'  # the retained owner is not positively stopped
        result = self.send('spec_review', 'ineligible-1')
        self.assertEqual(result['standing_policy']['outcome'], 'deferred')
        self.assertTrue(result['standing_policy']['reason'].startswith('audit:'),
                        result['standing_policy'])
        self.assertEqual(result['configured_model'], FABLE)
        self.assertEqual(result['state'], 'submitted')
        self.assertEqual(em.current(self.directory(), self.state())['configured_model'], FABLE)
        self.owner['activity_state'] = 'idle'
        self.native_turn('ineligible-1', FABLE)
        self.fake.finish('engineer', self.verdict())
        self.service.ao_room_sync(self.room)

    def test_apply_defers_an_audit_error_and_the_send_dispatches_on_the_pin(self):
        self.standing_room()
        self.set_policy()
        self.configure_target(NEWER)
        self.ready()
        with patch.object(et, 'audit', side_effect=ao.RoomError('boom')):
            result = self.send('spec_review', 'audit-raises')
        self.assertEqual(result['standing_policy']['outcome'], 'deferred')
        self.assertEqual(result['standing_policy']['reason'], 'audit:boom')
        self.assertEqual(result['standing_policy']['from'], FABLE)
        self.assertEqual(result['standing_policy']['to'], NEWER)
        self.assertEqual(result['configured_model'], FABLE)
        self.assertEqual(result['state'], 'submitted')
        self.assertEqual(em.current(self.directory(), self.state())['configured_model'], FABLE)
        application = ao.read(self.applications()[-1])
        self.assertEqual(application['outcome'], 'deferred')
        self.assertEqual(application['reason'], 'audit:boom')
        self.native_turn('audit-raises', FABLE)
        self.fake.finish('engineer', self.verdict())
        self.service.ao_room_sync(self.room)

    def test_apply_transitions_at_the_boundary_and_the_repeat_send_is_no_change(self):
        self.standing_room()
        self.set_policy()
        self.configure_target(NEWER)
        self.ready()
        self.patch_settings()
        result = self.send('spec_review', 'follow-1')
        self.assertEqual(result['standing_policy']['outcome'], 'transitioned')
        self.assertIsNone(result['standing_policy']['reason'])
        self.assertEqual(result['standing_policy']['from'], FABLE)
        self.assertEqual(result['standing_policy']['to'], NEWER)
        self.assertTrue(result['standing_policy']['request_id'].startswith('standing-'))
        self.assertTrue(result['standing_policy']['request_id'].endswith('-r9-' + NEWER))
        self.assertRegex(result['standing_policy']['record_sha256'] or '', r'^[0-9a-f]{64}$')
        self.assertEqual(result['configured_model'], NEWER)
        self.assertEqual(result['state'], 'submitted')
        self.assertEqual(self.request('follow-1')['standing_policy'], result['standing_policy'])
        epoch = em.current(self.directory(), self.state())
        self.assertEqual(epoch['selector'], {'kind': 'exact', 'model': NEWER})
        self.assertEqual(epoch['configured_model'], NEWER)
        attempt = em.journal(self.directory(), self.state())['committed'][-1]
        self.assertEqual(attempt['request_id'], result['standing_policy']['request_id'])
        self.assertEqual(attempt['intent_value']['inputs']['authorization'], AUTH)
        self.native_turn('follow-1', NEWER)
        self.fake.finish('engineer', self.verdict())
        self.service.ao_room_sync(self.room)
        repeat = self.send('spec_review', 'follow-2')
        self.assertEqual(repeat['standing_policy']['outcome'], 'no_change')
        self.assertEqual(repeat['configured_model'], NEWER)
        self.native_turn('follow-2', NEWER)
        self.fake.finish('engineer', self.verdict())
        self.service.ao_room_sync(self.room)

    def test_application_records_are_append_only_digest_named_and_chain_the_pointer(self):
        self.standing_room()
        self.set_policy()
        self.configure_target(FABLE)
        self.send('spec_review', 'app-1')
        self.native_turn('app-1', FABLE)
        self.fake.finish('engineer', self.verdict())
        self.service.ao_room_sync(self.room)
        self.send('spec_review', 'app-2')
        files = self.applications()
        self.assertEqual(len(files), 2)
        for path in files:
            value = ao.read(path)
            self.assertEqual(set(value), ep.APPLICATION_FIELDS)
            self.assertEqual(value['policy'], ep.POLICY)
            self.assertEqual(value['room_id'], self.state()['room_id'])
            self.assertEqual(ao.digest(value)[:12],
                             path.name.split('-')[-1].removesuffix('.json'))
        pointer = self.state()[ep.POINTER_KEY]
        self.assertEqual(pointer['last_application']['outcome'], 'no_change')
        self.assertEqual(pointer['last_application']['record'],
                         str(files[-1].relative_to(self.directory())))
        before = {path.name: path.read_bytes() for path in files}
        self.native_turn('app-2', FABLE)
        self.fake.finish('engineer', self.verdict())
        self.service.ao_room_sync(self.room)
        for path in files:
            self.assertEqual(path.read_bytes(), before[path.name])
        status = self.service.ao_room_status(self.room)['engineering_model_policy']
        self.assertEqual(status['last_application'],
                         {key: pointer['last_application'][key]
                          for key in ('outcome', 'reason', 'at', 'record_sha256')})


class ExposureTests(unittest.TestCase):
    """The operation's schema and its CLI/MCP surface."""

    def test_schema_requires_the_decision_fields_only(self):
        description, schema = ao.TOOL_SCHEMAS['ao_room_engineer_model_policy']
        self.assertIn('follow_newest_qualified_family_member', description)
        self.assertEqual(schema['required'], ['room_id', 'action', 'authorization'])
        self.assertEqual(set(schema['properties']), {'room_id', 'action', 'authorization', 'family'})
        self.assertEqual(schema['properties']['action']['enum'], ['set', 'revoke'])
        self.assertFalse(schema['additionalProperties'])
        self.assertIn('ao_room_engineer_model_policy', project_room.TOOL_SCHEMAS)


class CliTests(StandingPolicyFixture):
    """The CLI call round trip records the same audited decision."""

    def test_cli_call_records_the_policy(self):
        self.standing_room()
        output = io.StringIO()
        args = {'room_id': self.room, 'action': 'set', 'authorization': AUTH}
        with patch.object(project_room.signal, 'signal'), redirect_stdout(output):
            status = project_room.main(['--home', str(self.home), 'call', 'ao_room_engineer_model_policy',
                                        '--args', json.dumps(args)])
        self.assertEqual(status, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result['action'], 'set')
        self.assertTrue(result['active'])
        self.assertEqual(self.state()[ep.POINTER_KEY]['record'], result['record'])

    def test_mcp_lists_the_operation_with_write_annotations(self):
        service = project_room.Service(self.home)
        found = None
        for tool in project_room_mcp.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'},
                                            service)['result']['tools']:
            if tool['name'] == 'ao_room_engineer_model_policy':
                found = tool
        self.assertIsNotNone(found)
        self.assertEqual(found['annotations'],
                         {'readOnlyHint': False, 'destructiveHint': False, 'openWorldHint': False})


if __name__ == '__main__':
    unittest.main()
