"""Independent frozen-byte and real fake-Service migration/proof regressions."""
import copy
import contextlib
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import ao_project_room as ao
import ao_quality_review as quality
import ao_workflow
import test_ao_normal as fixtures
import test_ao_history_reconciliation as history_fixtures
import test_ao_review_extension as extension_fixtures


# Deliberately independent of production INSTRUCTION and its exported digest.
FROZEN = b'For supporting review work, use DeepSeek or another suitable delegate to prepare source-grounded facts, draft changes and evidence summaries. Have the assigned operator establish exact JSON paths, unique edit anchors, old values, hashes, schemas and test receipts with deterministic checks before Fable reviews the substantive changes. Fable may rely on verified mechanical results without reproducing each mechanical check, but must inspect the actual changes and original evidence needed to assess meaning, correctness, conflicts and omissions. Summaries and passing tests alone never establish approval; stale, incomplete or conflicting evidence requires investigation. Preserve approval of unchanged content only when its bytes, requirements and dependencies still match; review new semantic effects and interactions. For narrow corrections request only the changed items and preserve the unchanged complete result locally after verification. Fable retains MAX, specification review, pushback, enhancement judgment, routing/escalation discretion and the final engineering verdict. Necessary deeper review always takes priority over savings. Validate this allocation on the next bounded milestone using the same correctness and acceptance requirements; shorter prompts or fewer tokens alone are not success. This operating change grants no new scope, execution permission, recovery or review allowance.'
FROZEN_SHA = '6d043563ae4c27854dbaad53b9762476efa189c9fb6565dfb5670e24cee47a4e'
PART = 'quality_first_review_v1'


class FrozenTests(unittest.TestCase):
    def test_independent_exact_bytes_and_version(self):
        self.assertEqual(len(FROZEN), 1405)
        self.assertEqual(hashlib.sha256(FROZEN).hexdigest(), FROZEN_SHA)
        self.assertEqual(quality.INSTRUCTION.encode(), FROZEN)
        self.assertEqual(quality.INSTRUCTION_SHA256, FROZEN_SHA)
        self.assertEqual(quality.PART, PART)
        self.assertEqual((quality.MAX_FILE_BYTES, quality.MAX_TOTAL_BYTES, quality.MAX_REQUESTS,
                          quality.MAX_AMENDMENTS, quality.MAX_DEPTH), (8388608, 67108864, 4096, 256, 64))


class MigrationTests(fixtures.Fixture):
    def setUp(self):
        super().setUp()
        self.room = self.open(); self.spec(); self.bind()

    def stage(self, name, message=None):
        return self.service.ao_room_instruction_stage(self.room, name,
            FROZEN.decode() if message is None else message, 'Approved synthetic operating instruction')

    def quality_status(self):
        return self.service.ao_room_status(self.room)['engineer_context']['quality_first_review']

    def continue_request(self, name='continue', message='Continue.'):
        return self.service.ao_room_send(self.room, 'engineer', message, name, purpose='correction')

    def finish(self):
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)

    def restart(self):
        self.service = ao.Service(self.home, lambda url: self.fake)

    def old_session(self, amendments=()):
        for name, message in amendments:
            self.stage(name, message)
        # Exercise the real old packet and native receipt contracts: this is the
        # pre-promotion roster, not fabricated receipt authority.
        with patch.object(ao_workflow, 'PARTS', tuple(p for p in ao_workflow.PARTS if p != PART)):
            self.agree(); self.implement()

    def immutable_files(self):
        return {str(p.relative_to(self.directory())): hashlib.sha256(p.read_bytes()).hexdigest()
                for folder in ('receipts', 'instruction-amendments', 'specs')
                for p in (self.directory() / folder).rglob('*.json')}

    def test_initial_packet_and_exact_caller_only_after_restart(self):
        self.assertEqual(self.quality_status()['delivery'], 'undelivered')
        self.agree()
        request = self.state()['requests']['spec_review']
        self.assertEqual(request['text'].encode().count(FROZEN), 1)
        self.assertIn(PART, request['carried']['parts'])
        self.assertEqual(request['carried']['part_sha256'][PART], FROZEN_SHA)
        status = self.quality_status()
        self.assertEqual(status['delivery'], 'verified_delivered')
        self.assertEqual(status['source'], {'kind': 'workflow_part',
            'request_sha256': hashlib.sha256(b'spec_review').hexdigest(),
            'receipt_sha256': request['receipt_sha256'], 'amendment_sha256': None})
        self.restart(); self.implement()
        self.continue_request(message='Continue.\n  ')
        self.assertEqual(self.fake.posts[-1][1]['text'], 'Continue.\n  ')

    def test_retained_identical_pilot_is_a_verified_immutable_root(self):
        self.old_session([('pilot', FROZEN.decode())])
        before = self.immutable_files(); requests = copy.deepcopy(self.state()['requests'])
        self.restart(); self.continue_request()
        request = self.state()['requests']['continue']
        self.assertEqual(request['text'], 'Continue.')
        self.assertNotIn(PART, request['carried']['parts'])
        self.assertNotIn('instruction_amendments', request['carried'])
        self.finish(); self.restart()
        self.assertEqual(self.quality_status()['delivery'], 'verified_equivalent')
        self.assertEqual(self.quality_status()['source']['kind'], 'instruction_amendment')
        for key, value in before.items():
            self.assertEqual(hashlib.sha256((self.directory() / key).read_bytes()).hexdigest(), value)
        for key, value in requests.items():
            self.assertEqual(self.state()['requests'][key], value)

    def test_equivalent_delivery_keeps_legacy_carried_part_diagnostic_meaning(self):
        self.old_session([('pilot', FROZEN.decode())])
        context = self.service.ao_room_status(self.room)['engineer_context']
        self.assertIn(PART, context['undelivered_parts'])  # Legacy carried-part metadata only.
        self.assertNotIn(PART, context['parts'])
        self.assertEqual(context['quality_first_review']['delivery'], 'verified_equivalent')
        self.assertEqual(context['quality_first_review']['source']['kind'], 'instruction_amendment')

    def test_duplicate_staged_messages_group_with_initial_part(self):
        first, second = self.stage('one'), self.stage('two')
        self.agree()
        req = self.state()['requests']['spec_review']
        self.assertEqual(req['text'].encode().count(FROZEN), 1)
        self.assertIn(PART, req['carried']['parts'])
        self.assertEqual(req['carried']['instruction_amendments'], [first['sha256'], second['sha256']])
        self.assertNotIn(quality.EQUIVALENCE, req['carried'])
        self.restart(); self.implement()
        self.assertEqual(self.state()['requests']['implementation']['text'], 'Perform the exact authorized purpose.')
        self.assertEqual(self.quality_status()['equivalent_amendment_sha256'], sorted([first['sha256'], second['sha256']]))

    def test_held_part_satisfies_later_amendments_through_explicit_root(self):
        self.agree(); self.implement()
        root = self.quality_status()['source']
        before = self.immutable_files(); old_requests = copy.deepcopy(self.state()['requests'])
        first, second = self.stage('later-one'), self.stage('later-two')
        self.assertEqual(self.quality_status()['equivalent_amendment_sha256'], [])
        self.continue_request()
        req = self.state()['requests']['continue']
        self.assertEqual(req['text'], 'Continue.')
        self.assertEqual(req['carried']['parts'], [])
        self.assertNotIn('instruction_amendments', req['carried'])
        self.assertEqual(req['carried'][quality.EQUIVALENCE], {'version': 1, 'part': PART,
            'instruction_sha256': FROZEN_SHA, 'source': root,
            'amendment_sha256': sorted([first['sha256'], second['sha256']])})
        self.finish(); self.restart(); self.continue_request('again')
        self.assertEqual(self.state()['requests']['again']['text'], 'Continue.')
        self.assertNotIn(quality.EQUIVALENCE, self.state()['requests']['again']['carried'])
        for key, value in before.items():
            self.assertEqual(hashlib.sha256((self.directory() / key).read_bytes()).hexdigest(), value)
        for key, value in old_requests.items():
            self.assertEqual(self.state()['requests'][key], value)

    def test_retained_session_gets_new_instruction_once(self):
        self.old_session()
        self.continue_request()
        self.assertEqual(self.fake.posts[-1][1]['text'], FROZEN.decode() + '\nContinue.')
        self.finish(); self.restart(); self.continue_request('again')
        self.assertEqual(self.fake.posts[-1][1]['text'], 'Continue.')

    def test_distinct_messages_and_caller_bytes_keep_order(self):
        self.stage('before', 'Earlier unique instruction.')
        first = self.stage('identical')
        different = self.stage('different', FROZEN.decode() + ' ')
        self.stage('after', 'Later unique instruction.')
        self.agree()
        text = self.state()['requests']['spec_review']['text']
        suffix = ('Earlier unique instruction.\n' + FROZEN.decode() + ' \nLater unique instruction.\n'
                  'Perform the exact authorized purpose.')
        self.assertTrue(text.endswith(suffix))
        self.assertEqual(text.count(FROZEN.decode()), 2)
        self.assertEqual(self.quality_status()['equivalent_amendment_sha256'], [first['sha256']])
        self.assertNotIn(different['sha256'], self.quality_status()['equivalent_amendment_sha256'])

    def test_revision_delta_and_policy_migration_remain_once_only(self):
        self.agree()
        self.service.ao_room_spec_put(self.room, 2, 'Changed requirement.', self.gates, 'Approved revision')
        self.agree(revision=2, key='revision-two')
        req = self.state()['requests']['revision-two']
        self.assertEqual(req['carried']['spec_delivery'], 'changes')
        self.assertNotIn(FROZEN.decode(), req['text'])
        self.assertNotIn('<specification>', req['text'])
        self.assertIn('+Changed requirement.', req['text'])

    def test_caller_policy_text_alone_never_establishes_equivalence(self):
        self.old_session()
        with patch.object(ao_workflow, 'PARTS', tuple(p for p in ao_workflow.PARTS if p != PART)):
            self.continue_request('quoted-caller', FROZEN.decode()); self.finish()
        self.assertEqual(self.quality_status()['delivery'], 'undelivered')
        self.continue_request()
        self.assertEqual(self.fake.posts[-1][1]['text'], FROZEN.decode() + '\nContinue.')

    def test_one_byte_and_newline_differences_never_alias(self):
        messages = [FROZEN.decode() + '\n', FROZEN.decode() + ' ', FROZEN.decode().replace('MAX', 'max')]
        self.old_session([(str(i), message) for i, message in enumerate(messages)])
        self.assertEqual(self.quality_status()['delivery'], 'undelivered')
        self.continue_request(); self.finish(); self.restart()
        self.assertEqual(self.quality_status()['delivery'], 'verified_delivered')
        self.assertEqual(self.quality_status()['equivalent_amendment_sha256'], [])

    def test_pending_and_unknown_requests_never_prove_delivery_or_allow_send(self):
        self.send('spec_review')
        original = (self.directory() / 'state.json').read_bytes()
        self.assertEqual(self.quality_status()['delivery'], 'undelivered')
        posts = len(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, 'active or uncertain'):
            self.send('spec_review', 'forbidden')
        self.assertEqual((self.directory() / 'state.json').read_bytes(), original)
        self.assertEqual(len(self.fake.posts), posts)
        self.fake.finish('engineer', state='failed'); self.service.ao_room_sync(self.room)
        self.assertEqual(self.quality_status()['delivery'], 'undelivered')
        with self.assertRaisesRegex(ao.RoomError, 'active or uncertain'):
            self.send('spec_review', 'still-forbidden')
        self.assertEqual(len(self.fake.posts), posts)

    def test_historical_canonical_packet_migrates_without_rewriting_evidence(self):
        self.send('spec_review')
        state = self.state(); req = state['requests']['spec_review']
        spec = self.service.spec(self.directory(), state)
        req.pop('carried'); req.pop('prompt_projection')
        req['text'] = ('Historical workflow.\nExact specification revision 1, SHA256 ' + spec['sha256']
                       + '\n<specification>\n' + spec['content'] + '\n</specification>\nAgreed gates: '
                       + json.dumps(spec['gates']) + '\nTask instruction:\nReview.')
        req['text_sha256'] = hashlib.sha256(req['text'].encode()).hexdigest()
        self.service.save(self.directory(), state)
        self.fake.snapshots['engineer']['messages'][-1]['text'] = req['text']
        self.fake.finish('engineer', json.dumps({'interpretation': 'Exact scope', 'findings': [],
            'decision': 'accept', 'spec_revision': 1, 'spec_sha256': spec['sha256']}))
        self.service.ao_room_sync(self.room)
        self.assertEqual(self.quality_status()['delivery'], 'undelivered')
        before = self.immutable_files()
        self.restart(); self.implement()
        self.assertEqual(self.state()['requests']['implementation']['text'].count(FROZEN.decode()), 1)
        self.continue_request()
        self.assertEqual(self.fake.posts[-1][1]['text'], 'Continue.')
        for key, value in before.items():
            self.assertEqual(hashlib.sha256((self.directory() / key).read_bytes()).hexdigest(), value)

    def test_verified_settled_failure_holds_delivery_but_does_not_release_gate(self):
        import ao_native_outcome
        self.old_session(); self.stage('pilot')
        with patch.object(ao_workflow, 'PARTS', tuple(p for p in ao_workflow.PARTS if p != PART)):
            self.continue_request('failed-pilot')
        self.fake.finish('engineer', state='failed'); self.service.ao_room_sync(self.room)
        state = self.state(); state['native_outcome_source'] = {'session_id': 'engineer'}
        self.service.save(self.directory(), state)
        self.assertEqual(self.quality_status()['delivery'], 'undelivered')
        observed = {'anchor_uuid': 'synthetic-packet', 'next_human_uuid': None,
                    'errors': [{'uuid': 'synthetic-quota', 'error': 'rate_limit', 'http_status': 429}],
                    'stop_reasons': [], 'source_sha256': 'f' * 64}
        with patch.object(ao_native_outcome, 'inspect', return_value=observed):
            audit = self.service.ao_room_outcome_audit(self.room)
            self.service.ao_room_outcome_resume(self.room, 'failed-pilot', audit['outcome_sha256'], 'resume',
                                               'Synthetic user confirmed capacity after native quota', 'Continue once')
            before = self.immutable_files()
            self.restart()
            self.assertEqual(self.quality_status()['delivery'], 'verified_equivalent')
            posts = len(self.fake.posts)
            with self.assertRaisesRegex(ao.RoomError, 'semantic hold'):
                self.continue_request('unauthorized-resume')
            self.assertEqual(len(self.fake.posts), posts)
            self.continue_request('resume')
            self.assertEqual(self.fake.posts[-1][1]['text'], 'Continue.')
            request = self.state()['requests']['failed-pilot']
            self.assertEqual(request['state'], 'settled_failure')
            self.assertEqual(ao.read(self.directory() / request['receipt'])['turn']['state'], 'failed')
            for key, value in before.items():
                self.assertEqual(hashlib.sha256((self.directory() / key).read_bytes()).hexdigest(), value)

    def test_single_prompt_assembly_metrics_and_unchanged_role_budget_fields(self):
        import ao_prompt_metrics
        state = self.state()
        init = ao_prompt_metrics.PromptAssembly.__init__
        count = []
        def tracked(instance, *args, **kwargs):
            count.append(1)
            return init(instance, *args, **kwargs)
        with patch.object(ao_prompt_metrics.PromptAssembly, '__init__', tracked):
            self.agree()
        self.assertEqual(len(count), 1)
        request = self.state()['requests']['spec_review']
        projection = request['prompt_projection']
        text = self.fake.posts[-1][1]['text']
        self.assertEqual(projection['text_sha256'], hashlib.sha256(text.encode()).hexdigest())
        self.assertEqual(projection['total_bytes'], len(text.encode()))
        self.assertEqual(sum(projection[k] for k in ('caller_bytes', 'specification_bytes', 'workflow_bytes', 'separator_bytes')),
                         projection['total_bytes'])
        for key in ('bindings', 'delegate', 'authorization'):
            self.assertEqual(self.state()[key], state[key])


class IntegrityTests(fixtures.Fixture):
    def setUp(self):
        super().setUp()
        self.room = self.open(); self.spec(); self.bind(); self.agree(); self.implement()

    def projection(self):
        return quality.summary(self.directory(), self.state())

    def assert_refused(self, reason='quality_evidence_integrity'):
        before = {str(p.relative_to(self.directory())): p.read_bytes()
                  for p in self.directory().rglob('*.json') if not p.is_symlink()}
        posts = len(self.fake.posts)
        result = self.projection()
        self.assertEqual(result['delivery'], 'unavailable_integrity')
        self.assertEqual(result['reasons'], [reason])
        with patch.object(self.service, 'save', side_effect=AssertionError('No save on quality refusal')):
            with self.assertRaisesRegex(ao.RoomError, 'quality_evidence_'):
                self.service.ao_room_send(self.room, 'engineer', 'Continue.', 'refused', purpose='correction')
        self.assertEqual(len(self.fake.posts), posts)
        after = {str(p.relative_to(self.directory())): p.read_bytes()
                 for p in self.directory().rglob('*.json') if not p.is_symlink()}
        self.assertEqual(before, after)

    def rewrite_receipt(self, state, request, receipt):
        # A synthetic coherent receipt rewrite tests structural checks beyond a
        # bare digest mismatch. No real room or accepted evidence is used here.
        def canonical(value):
            return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                            ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        request['receipt'] = 'receipts/' + request['request_id'] + '/' + canonical({
            k: v for k, v in receipt.items() if k != 'observed_at'}) + '.json'
        request['receipt_sha256'] = canonical(receipt)
        ao.atomic(self.directory() / request['receipt'], receipt)
        self.service.save(self.directory(), state)

    def test_missing_receipt_and_tampered_receipt_refuse_without_rewrite(self):
        request = self.state()['requests']['spec_review']
        path = self.directory() / request['receipt']; original = path.read_bytes()
        path.unlink(); self.assert_refused('quality_evidence_unavailable')
        path.write_bytes(original + b'garbage'); self.assert_refused()

    def test_changed_amendment_and_foreign_room_session_and_version_refuse(self):
        pointer = self.service.ao_room_instruction_stage(self.room, 'pilot', FROZEN.decode(), 'Approved synthetic test')
        path = self.directory() / pointer['path']; original = ao.read(path)
        for change in ({'message': FROZEN.decode() + ' '}, {'room_id': 'another-room'},
                       {'session_id': 'foreign-session'}, {'version': True}, {'request_id': 'another-request'}):
            with self.subTest(change=list(change)):
                value = {**original, **change}; ao.atomic(path, value)
                self.assert_refused()

    def test_missing_amendment_source_refuses(self):
        pointer = self.service.ao_room_instruction_stage(self.room, 'pilot', FROZEN.decode(), 'Approved')
        (self.directory() / pointer['path']).unlink()
        self.assert_refused('quality_evidence_unavailable')

    def test_modified_carried_metadata_refuses(self):
        state = self.state()
        state['requests']['spec_review']['carried']['part_sha256'][PART] = 'e' * 64
        self.service.save(self.directory(), state)
        self.assert_refused()

    def projection_binding_refusal(self, missing):
        state = self.state(); request = state['requests']['spec_review']
        receipt = ao.read(self.directory() / request['receipt'])
        if missing:
            receipt.pop('prompt_projection_sha256')
        else:
            receipt['prompt_projection_sha256'] = '0' * 64
        self.rewrite_receipt(state, request, receipt)
        before = (self.directory() / 'state.json').read_bytes(); posts = len(self.fake.posts)
        result = self.projection(); error = None
        try:
            self.service.ao_room_send(self.room, 'engineer', 'Continue.', 'projection-refused', purpose='correction')
        except ao.RoomError as exc:
            error = str(exc)
        sent = len(self.fake.posts) - posts
        self.assertEqual(result['delivery'], 'unavailable_integrity',
                         'Contradictory projection: status=' + result['delivery'] + ', actual fake dispatches=' + str(sent))
        self.assertIn('quality_evidence_integrity', error or '')
        self.assertEqual(sent, 0)
        self.assertEqual((self.directory() / 'state.json').read_bytes(), before)

    def test_missing_modern_projection_binding_refuses_status_and_send(self):
        self.projection_binding_refusal(missing=True)

    def test_tampered_modern_projection_binding_refuses_status_and_send(self):
        self.projection_binding_refusal(missing=False)

    def test_policy_digest_without_actual_text_never_proves_a_root(self):
        state = self.state(); request = state['requests']['spec_review']
        receipt = ao.read(self.directory() / request['receipt'])
        request['text'] = request['text'].replace(FROZEN.decode(), 'One byte different instruction.')
        request['text_sha256'] = hashlib.sha256(request['text'].encode()).hexdigest()
        request.pop('prompt_projection')
        receipt.pop('prompt_projection_sha256')
        for message in receipt['messages']:
            if message['role'] == 'user': message['text'] = request['text']
        self.rewrite_receipt(state, request, receipt)
        self.assert_refused()

    def test_carried_amendment_without_text_or_pointer_is_not_delivery(self):
        pointer = self.service.ao_room_instruction_stage(self.room, 'different', 'Absent unique instruction.', 'Approved')
        state = self.state(); req = state['requests']['implementation']
        req['carried']['instruction_amendments'] = [pointer['sha256']]
        receipt = ao.read(self.directory() / req['receipt'])
        receipt['carried_sha256'] = ao.digest(req['carried'])
        self.rewrite_receipt(state, req, receipt)
        self.assert_refused()
        state.pop('instruction_amendments'); self.service.save(self.directory(), state)
        self.assert_refused()

    def test_coherent_receipt_with_contradictory_identity_refuses(self):
        original = self.state()
        for target, field, value in [('request', 'session_id', 'foreign'), ('request', 'model', 'foreign'),
                                     ('request', 'reasoning_effort', 'low'), ('request', 'conversation_id', 'foreign'),
                                     ('turn', 'providerTurnId', 'foreign'), ('turn', 'id', 'foreign'),
                                     ('turn', 'state', 'running')]:
            with self.subTest(target=target, field=field):
                state = copy.deepcopy(original); req = state['requests']['spec_review']
                receipt = ao.read(self.directory() / req['receipt'])
                if target == 'request': req[field] = value
                else: receipt['turn'][field] = value
                self.rewrite_receipt(state, req, receipt)
                self.assert_refused()
        self.service.save(self.directory(), original)

    def test_ambiguous_exact_message_and_duplicate_order_refuse(self):
        state = self.state(); req = state['requests']['spec_review']
        receipt = ao.read(self.directory() / req['receipt'])
        receipt['messages'].append(copy.deepcopy(next(m for m in receipt['messages'] if m['role'] == 'user')))
        self.rewrite_receipt(state, req, receipt); self.assert_refused()
        state['requests']['implementation']['created_order'] = req['created_order']
        self.service.save(self.directory(), state); self.assert_refused()

    def test_equivalence_reverifies_original_root_and_rejects_derived_sources(self):
        self.service.ao_room_instruction_stage(self.room, 'later', FROZEN.decode(), 'Approved')
        self.service.ao_room_send(self.room, 'engineer', 'Continue.', 'later-send', purpose='correction')
        self.fake.finish('engineer', json.dumps(self.report())); self.service.ao_room_sync(self.room)
        original = self.state(); req = original['requests']['later-send']
        value = req['carried'][quality.EQUIVALENCE]
        for source in ({**value['source'], 'request_sha256': hashlib.sha256(b'later-send').hexdigest(),
                        'receipt_sha256': req['receipt_sha256']},
                       {**value['source'], 'receipt_sha256': '0' * 64},
                       {**value['source'], 'amendment_sha256': '0' * 64}):
            with self.subTest(source=source):
                state = copy.deepcopy(original); changed = state['requests']['later-send']
                changed['carried'][quality.EQUIVALENCE]['source'] = source
                receipt = ao.read(self.directory() / changed['receipt'])
                receipt['carried_sha256'] = ao.digest(changed['carried'])
                self.rewrite_receipt(state, changed, receipt); self.assert_refused()
        self.service.save(self.directory(), original)
        root = original['requests']['spec_review']
        (self.directory() / root['receipt']).unlink()
        self.assert_refused('quality_evidence_unavailable')

    def test_strict_json_and_depth_limits_are_closed_unavailable_results(self):
        path = self.directory() / 'state.json'; raw = path.read_bytes(); state = self.state()
        variants = [raw[:-2] + b', "room_id": "duplicate"}', b'\xff',
                    b'{"bad":"\\ud800"}', b'{"bad":NaN}', b'{"bad":1e9999}',
                    b'{"bad":' + b'[' * 64 + b'0' + b']' * 64 + b'}']
        for index, damaged in enumerate(variants):
            with self.subTest(index=index):
                path.write_bytes(damaged)
                result = quality.summary(self.directory(), state)
                self.assertEqual(result['delivery'], 'unavailable_integrity')
                self.assertEqual(result['reasons'], ['quality_evidence_limit' if index == 5 else 'quality_evidence_integrity'])
                self.assertEqual(path.read_bytes(), damaged)
        path.write_bytes(raw)

    def test_request_amendment_and_binding_byte_bounds_refuse(self):
        original = self.state()
        for field, value in [('requests', {str(i): {} for i in range(4097)}),
                             ('instruction_amendments', [{}] * 257)]:
            with self.subTest(field=field):
                state = copy.deepcopy(original); state[field] = value; self.service.save(self.directory(), state)
                self.assertEqual(quality.summary(self.directory(), state)['reasons'], ['quality_evidence_limit'])
        self.service.save(self.directory(), original)
        request = original['requests']['spec_review']; path = self.directory() / request['receipt']
        with path.open('r+b') as stream: stream.truncate(8388609)
        self.assert_refused('quality_evidence_limit')

    def test_receipt_and_amendment_symlinks_and_hardlinks_refuse(self):
        req = self.state()['requests']['spec_review']; path = self.directory() / req['receipt']
        original = path.read_bytes(); outside = self.root / 'outside.json'; outside.write_bytes(original)
        path.unlink(); path.symlink_to(outside); self.assert_refused()
        path.unlink(); os.link(outside, path); self.assert_refused()
        path.unlink(); path.write_bytes(original)
        pointer = self.service.ao_room_instruction_stage(self.room, 'later', FROZEN.decode(), 'Approved')
        amendment = self.directory() / pointer['path']; saved = amendment.read_bytes(); amendment.unlink()
        outside.write_bytes(saved); amendment.symlink_to(outside); self.assert_refused()

    def test_receipt_parent_symlink_and_owned_path_escape_refuse(self):
        state = self.state(); req = state['requests']['spec_review']; path = self.directory() / req['receipt']
        parent = path.parent; renamed = self.root / 'relocated-receipts'; parent.rename(renamed)
        parent.symlink_to(renamed, target_is_directory=True)
        self.assert_refused()
        parent.unlink(); renamed.rename(parent)
        req['receipt'] = '../' + path.name; self.service.save(self.directory(), state)
        self.assert_refused()

    def test_summary_is_closed_local_readonly_and_never_calls_service_or_processes(self):
        import socket
        import subprocess
        state = self.state(); before = (self.directory() / 'state.json').read_bytes()
        with (patch.object(ao, 'Service', side_effect=AssertionError('No mutable Service')),
              patch.object(subprocess, 'run', side_effect=AssertionError('No subprocess')),
              patch.object(socket, 'socket', side_effect=AssertionError('No network'))):
            result = quality.summary(self.directory(), state)
        self.assertEqual(result['delivery'], 'verified_delivered')
        self.assertEqual(set(result), {'version', 'part', 'instruction_sha256', 'delivery', 'source',
                                      'equivalent_amendment_sha256', 'reasons'})
        self.assertEqual(set(result['source']), {'kind', 'request_sha256', 'receipt_sha256', 'amendment_sha256'})
        encoded = json.dumps(result)
        for private in (str(self.directory()), FROZEN.decode(), 'spec_review', 'engineer'):
            self.assertNotIn(private, encoded)
        self.assertEqual((self.directory() / 'state.json').read_bytes(), before)


class ReconciledQualityTests(unittest.TestCase):
    def setUp(self):
        # Compose the existing real reconciliation fixture without inheriting and
        # collecting its entire TestCase a second time.
        self.fixture = history_fixtures.HistoryReconciliationTests(
            'test_explicit_audit_release_and_exactly_one_successor_preserve_receipt')
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    def projection(self):
        f = self.fixture
        return quality.summary(f.directory(), f.state())

    def test_verified_reconciliation_allows_only_released_caller_once_after_restart(self):
        f = self.fixture
        self.assertEqual(self.projection()['delivery'], 'unavailable_integrity')
        before_posts = len(f.fake.posts)
        audited = f.audit()
        self.assertEqual(self.projection()['delivery'], 'verified_delivered')
        self.assertEqual(len(f.fake.posts), before_posts)
        self.assertTrue(f.current()['semantic_status']['hold'])
        f.release(audited); f.assert_preserved()
        f.service = ao.Service(f.home, lambda url: f.fake)
        actual = f.service.ao_room_status(f.room)['engineer_context']['quality_first_review']
        self.assertEqual(actual['delivery'], 'verified_delivered')
        f.send('correction', 'continue-once'); f.send('correction', 'continue-once')
        self.assertEqual(len(f.fake.posts), before_posts + 1)
        self.assertEqual(f.fake.posts[-1][1]['text'], 'Perform the exact authorized purpose.')
        f.assert_preserved()

    def test_reconciliation_records_are_owned_readonly_and_fail_closed(self):
        from ao_history_reconciliation import PROOF, INVALIDATION
        f = self.fixture
        f.release(f.audit())
        f.snapshot['messages'][-1]['text'] = 'Changed'
        f.service.ao_room_sync(f.room)
        f.snapshot['messages'][-1]['text'] = ''
        f.release(f.audit())
        request = f.current()
        paths = [f.directory() / 'history-reconciliations' / 'implementation' / (request[PROOF] + '.json'),
                 f.directory() / request['semantic_outcome'],
                 f.directory() / 'history-reconciliation-invalidations' / 'implementation' /
                 (request[INVALIDATION] + '.json')]
        state_bytes = (f.directory() / 'state.json').read_bytes()
        for path in paths:
            raw = path.read_bytes()
            for mode, reason in (('missing', 'quality_evidence_unavailable'),
                                 ('modified', 'quality_evidence_integrity'),
                                 ('symlink', 'quality_evidence_integrity')):
                with self.subTest(area=path.parent.parent.name, mode=mode):
                    if mode == 'modified':
                        path.write_text('{}\n')
                    else:
                        path.unlink()
                        if mode == 'symlink':
                            target = f.root / 'foreign-evidence.json'
                            target.write_bytes(raw); path.symlink_to(target)
                    result = self.projection()
                    self.assertEqual(result['delivery'], 'unavailable_integrity')
                    self.assertEqual(result['reasons'], [reason])
                    self.assertEqual((f.directory() / 'state.json').read_bytes(), state_bytes)
                    if mode == 'symlink': path.unlink()
                    path.write_bytes(raw)
                    self.assertEqual(self.projection()['delivery'], 'verified_delivered')
        f.assert_preserved()

    def test_coherently_rehashed_reconciliation_must_match_saved_delivery(self):
        from ao_history_reconciliation import PROOF
        f = self.fixture
        f.release(f.audit())
        original = f.state(); request = original['requests']['implementation']
        proof = ao.read(f.directory() / 'history-reconciliations' / 'implementation' / (request[PROOF] + '.json'))
        outcome = ao.read(f.directory() / request['semantic_outcome'])
        for key in ('session_id', 'baseline_sha256', 'messages_sha256', 'ao_terminal_sha256'):
            with self.subTest(key=key):
                altered = copy.deepcopy(proof)
                altered['inputs'][key] = 'foreign-session' if key == 'session_id' else '0' * 64
                sha = ao.digest(altered)
                ao.atomic(f.directory() / 'history-reconciliations' / 'implementation' / (sha + '.json'), altered)
                changed = copy.deepcopy(outcome); changed[PROOF] = sha
                outcome_sha = ao.digest(changed)
                path = 'outcomes/implementation/' + outcome_sha + '.json'
                ao.atomic(f.directory() / path, changed)
                state = copy.deepcopy(original)
                state['requests']['implementation'].update({PROOF: sha, 'semantic_outcome': path,
                                                            'semantic_outcome_sha256': outcome_sha})
                ao.atomic(f.directory() / 'state.json', state)
                before = (f.directory() / 'state.json').read_bytes()
                self.assertEqual(self.projection()['delivery'], 'unavailable_integrity')
                self.assertEqual((f.directory() / 'state.json').read_bytes(), before)
        ao.atomic(f.directory() / 'state.json', original)
        f.assert_preserved()

    def test_refused_send_preserves_existing_invalidation_authority(self):
        from ao_history_reconciliation import PROOF, INVALIDATION
        f = self.fixture
        audited = f.audit(); f.release(audited)
        before = f.state(); request = f.current(); posts = len(f.fake.posts)
        proof = f.directory() / 'history-reconciliations' / 'implementation' / (request[PROOF] + '.json')
        raw = proof.read_bytes(); proof.write_text('{}\n')
        with self.assertRaisesRegex(ao.RoomError, 'quality_evidence_integrity'):
            f.send('correction', 'continue-once')
        after = f.state()
        self.assertEqual(set(after['requests']), set(before['requests']))
        self.assertEqual(len(f.fake.posts), posts)
        # Service.locked has preexisting authority to invalidate an established
        # history proof on any refusal; the read-only verifier does not suppress it.
        self.assertIn(INVALIDATION, after['requests']['implementation'])
        self.assertNotEqual(after, before)
        proof.write_bytes(raw)
        self.assertEqual(self.projection()['delivery'], 'unavailable_integrity')
        f.assert_stale_requires_new_audit(audited)
        self.assertEqual(self.projection()['delivery'], 'verified_delivered')
        f.send('correction', 'continue-once')
        self.assertEqual(len(f.fake.posts), posts + 1)
        f.assert_preserved()

    def test_reconciled_status_uses_only_saved_local_proof(self):
        import socket
        import subprocess
        f = self.fixture
        f.release(f.audit()); state = f.state()
        before = (f.directory() / 'state.json').read_bytes()
        with (patch.object(ao, 'Service', side_effect=AssertionError('No mutable Service')),
              patch('ao_native_outcome.inspect', side_effect=AssertionError('No native transcript inspection')),
              patch('ao_history_reconciliation.reconcile', side_effect=AssertionError('No history audit')),
              patch.object(subprocess, 'run', side_effect=AssertionError('No subprocess')),
              patch.object(socket, 'socket', side_effect=AssertionError('No network'))):
            result = quality.summary(f.directory(), state)
        self.assertEqual(result['delivery'], 'verified_delivered')
        self.assertEqual((f.directory() / 'state.json').read_bytes(), before)
        for private in (str(f.root), f.source['native_session_id'], 'implementation'):
            self.assertNotIn(private, json.dumps(result))
        f.assert_preserved()


class HistoricalSourceQualityTests(unittest.TestCase):
    setUp = ReconciledQualityTests.setUp
    projection = ReconciledQualityTests.projection

    def room_files(self):
        directory = self.fixture.directory()
        return {str(path.relative_to(directory)): path.read_bytes()
                for path in directory.rglob('*') if path.is_file()}

    def complete_successor(self):
        f = self.fixture
        f.release(f.audit())
        f.service.ao_room_send(f.room, 'engineer', 'Complete the one authorized successor.',
                               'continue-once', purpose='correction')
        successor = f.state()['requests']['continue-once']
        stamp = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat()
        f.events.extend([
            {'type': 'user', 'uuid': 'successor-caller', 'sessionId': 'native-fixture',
             'timestamp': stamp(successor['created_at'] + 0.1), 'origin': {'kind': 'human'},
             'message': {'content': successor['text']}},
            {'type': 'assistant', 'uuid': 'successor-final', 'sessionId': 'native-fixture',
             'timestamp': stamp(successor['created_at'] + 0.2),
             'message': {'model': successor['model'], 'id': 'successor-response', 'stop_reason': 'end_turn'}},
        ])
        f.write_native()
        (f.repo / 'feature.txt').write_text('implemented\n')
        f.fake.finish('engineer', json.dumps(f.report()))
        f.service.ao_room_sync(f.room)
        self.assertEqual(f.state()['requests']['continue-once']['semantic_status']['kind'], 'final_available')

    def move_source(self):
        f = self.fixture
        moved_directory = f.root / 'moved-source'; moved_directory.mkdir()
        moved = moved_directory / 'native-fixture.jsonl'
        moved.write_bytes(f.transcript.read_bytes())
        result = f.service.ao_room_outcome_audit(f.room, ao_database_path=f.source['database'],
                                                native_transcript_path=str(moved))
        self.assertEqual(result['request_id'], 'continue-once')
        self.assertEqual(result['outcome']['kind'], 'final_available')
        self.assertIs(result['outcome']['hold'], False)
        self.assertEqual(f.state()['native_outcome_source']['transcript'], str(moved))
        return moved

    def test_audited_latest_source_move_keeps_historical_delivery_and_caller_only_followup(self):
        f = self.fixture
        self.complete_successor()
        original_request = copy.deepcopy(f.current())
        historical_files = {key: value for key, value in self.room_files().items()
                            if key.startswith(('receipts/implementation/', 'history-reconciliations/implementation/',
                                               'outcomes/implementation/', 'outcome-resumes/implementation'))}
        before_status = self.room_files()
        self.assertEqual(f.service.ao_room_status(f.room)['engineer_context']['quality_first_review']['delivery'],
                         'verified_delivered')
        self.assertEqual(self.room_files(), before_status)
        self.move_source()
        f.transcript.unlink()  # The historical proof cannot depend on an old live path still existing.
        f.service = ao.Service(f.home, lambda url: f.fake)
        after_move = self.room_files()
        result = f.service.ao_room_status(f.room)['engineer_context']['quality_first_review']
        self.assertEqual(result['delivery'], 'verified_delivered')
        self.assertEqual(self.room_files(), after_move)
        self.assertEqual(f.current(), original_request)
        posts = len(f.fake.posts)
        for _ in range(2):
            f.service.ao_room_send(f.room, 'engineer', 'Inspect and correct the completed result.',
                                  'post-move-correction', purpose='correction')
        self.assertEqual(len(f.fake.posts), posts + 1)
        self.assertEqual(f.fake.posts[-1][1]['text'], 'Inspect and correct the completed result.')
        self.assertEqual(f.current(), original_request)
        current_files = self.room_files()
        self.assertTrue(all(current_files[key] == value for key, value in historical_files.items()))
        f.assert_preserved()

    def test_historical_source_identity_and_proof_outcome_disagreement_refuse(self):
        from ao_history_reconciliation import PROOF
        f = self.fixture
        self.complete_successor(); self.move_source()
        original = f.state(); request = original['requests']['implementation']
        proof = ao.read(f.directory() / 'history-reconciliations' / 'implementation' / (request[PROOF] + '.json'))
        outcome = ao.read(f.directory() / request['semantic_outcome'])
        posts = len(f.fake.posts)
        for mode in ('foreign-session', 'foreign-native', 'relative-transcript', 'extra-source-key', 'proof-only-path'):
            with self.subTest(mode=mode):
                altered = copy.deepcopy(proof); source = altered['inputs']['native']['source']
                if mode == 'foreign-session': source['session_id'] = 'foreign-session'
                elif mode == 'foreign-native': source['native_session_id'] = 'foreign-native'
                elif mode == 'relative-transcript': source['transcript'] = 'native-fixture.jsonl'
                elif mode == 'extra-source-key': source['unverified_owner'] = 'foreign'
                else: source['transcript'] = str(f.root / 'unverified-move' / 'native-fixture.jsonl')
                sha = ao.digest(altered)
                ao.atomic(f.directory() / 'history-reconciliations' / 'implementation' / (sha + '.json'), altered)
                changed = copy.deepcopy(outcome); changed[PROOF] = sha
                if mode != 'proof-only-path': changed['native'] = copy.deepcopy(altered['inputs']['native'])
                outcome_sha = ao.digest(changed); path = 'outcomes/implementation/' + outcome_sha + '.json'
                ao.atomic(f.directory() / path, changed)
                state = copy.deepcopy(original)
                state['requests']['implementation'].update({PROOF: sha, 'semantic_outcome': path,
                                                            'semantic_outcome_sha256': outcome_sha})
                ao.atomic(f.directory() / 'state.json', state)
                before = self.room_files()
                self.assertEqual(self.projection()['delivery'], 'unavailable_integrity')
                with self.assertRaisesRegex(ao.RoomError, 'quality_evidence_integrity'):
                    f.service.ao_room_send(f.room, 'engineer', 'Continue.', 'refused-source', purpose='correction')
                self.assertEqual(self.room_files(), before)
                self.assertEqual(len(f.fake.posts), posts)
        ao.atomic(f.directory() / 'state.json', original)
        self.assertEqual(self.projection()['delivery'], 'verified_delivered')
        f.assert_preserved()

    def test_latest_source_drift_requires_explicit_reaudit_and_release(self):
        f = self.fixture
        f.release(f.audit())
        moved_directory = f.root / 'moved-source'; moved_directory.mkdir()
        moved = moved_directory / 'native-fixture.jsonl'; moved.write_bytes(f.transcript.read_bytes())
        state = f.state(); state['native_outcome_source']['transcript'] = str(moved)
        ao.atomic(f.directory() / 'state.json', state)  # Synthetic unaudited latest-source drift.
        self.assertEqual(self.projection()['delivery'], 'unavailable_integrity')
        posts = len(f.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, 'quality_evidence_integrity'):
            f.send('correction', 'continue-once')
        self.assertEqual(len(f.fake.posts), posts)
        audited = f.service.ao_room_outcome_audit(f.room, ao_database_path=f.source['database'],
                                                  native_transcript_path=str(moved))
        self.assertTrue(audited['resume_eligible'])
        f.release(audited); f.send('correction', 'continue-once')
        self.assertEqual(len(f.fake.posts), posts + 1)
        f.assert_preserved()

    def test_historical_delivery_does_not_bypass_latest_native_owner_hold(self):
        f = self.fixture
        self.complete_successor(); self.move_source()
        original = copy.deepcopy(f.current()); posts = len(f.fake.posts)
        f.owner['provider_conversation_id'] = 'foreign-native-owner'
        self.assertEqual(self.projection()['delivery'], 'verified_delivered')
        with self.assertRaisesRegex(ao.RoomError, 'Native semantic hold'):
            f.service.ao_room_send(f.room, 'engineer', 'Continue.', 'held-source', purpose='correction')
        self.assertEqual(len(f.fake.posts), posts)
        state = f.state()
        self.assertNotIn('held-source', state['requests'])
        self.assertIs(state['requests']['continue-once']['semantic_status']['hold'], True)
        self.assertEqual(f.current(), original)
        f.assert_preserved()


class FreshLedgerFixture(fixtures.DelegateFixture):
    make_database = extension_fixtures.ReviewExtensionFixture.make_database
    register = extension_fixtures.ReviewExtensionFixture.register
    audit = extension_fixtures.ReviewExtensionFixture.audit
    grant_inputs = extension_fixtures.ReviewExtensionFixture.grant_inputs
    grant = extension_fixtures.ReviewExtensionFixture.grant
    finish_fourth = extension_fixtures.ReviewExtensionFixture.finish_fourth

    def setUp(self):
        super().setUp()
        original = self.fake.request
        def paged(method, path, payload=None):
            if method == 'GET' and '/conversation?' in path:
                return {**self.fake.conversation(path.split('/')[2]), 'hasMoreBefore': False}
            return original(method, path, payload)
        self.fake.request = paged
        for session in self.fake.sessions.values():
            session['isTerminated'] = False
        for snapshot in self.fake.snapshots.values():
            snapshot.update(controller='ready', branchMaterialization={'strategy': 'native', 'replayTruncated': False})
        self.bind()
        for number in range(1, 4):
            self.agree(key='charter-' + str(number))
        self.implement(); self.review()
        self.accepted = self.service.ao_room_accept(self.room, 'acceptance_review')
        self.make_database(); self.target_spec = None


class ExtensionQualityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = FreshLedgerFixture('runTest')
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    def test_first_ledger_observation_keeps_one_authorized_fourth_review(self):
        import deepseek_adapter
        f = self.fixture
        grant = f.grant()
        before = f.state()
        self.assertEqual(before['delegate']['provider'], 'deepseek')
        self.assertNotIn('delegate_ledger_observed', before)
        self.assertFalse((f.home / 'deepseek' / 'ledger.sqlite3').exists())
        deepseek_adapter.Ledger(f.home)  # Local empty synthetic ledger, no worker invocation.
        posts = len(f.fake.posts)
        self.assertEqual(f.send('spec_review', 'fourth-charter')['state'], 'submitted')
        self.assertEqual(f.send('spec_review', 'fourth-charter')['state'], 'submitted')
        self.assertEqual(len(f.fake.posts), posts + 1)
        after = f.state()
        self.assertIs(after['delegate_ledger_observed'], True)
        self.assertEqual(after['spec_review_extension'], before['spec_review_extension'])
        self.assertEqual(after['requests']['fourth-charter']['carried']['spec_review_extension_sha256'],
                         grant['receipt_sha256'])
        self.assertEqual(after['requests']['fourth-charter']['text'].count(FROZEN.decode()), 0)
        for key, request in before['requests'].items():
            self.assertEqual(after['requests'][key], request)
        status = f.finish_fourth()
        self.assertEqual(status['spec_review_extension']['remaining_spec_reviews'], 0)
        self.assertEqual(status['engineer_context']['quality_first_review']['delivery'], 'verified_delivered')
        with self.assertRaisesRegex(ao.RoomError, 'fourth.*exhausted'):
            f.send('spec_review', 'fifth-charter')
        self.assertEqual(len(f.fake.posts), posts + 1)

    def test_amendment_inspection_retains_proof_drift_and_full_inspector_checks(self):
        from ao_instruction_amendments import pending
        f = self.fixture
        pointer = f.service.ao_room_instruction_stage(f.room, 'new-amendment',
            'A distinct authorized operating instruction.', 'Synthetic authorization')
        f.grant()
        state = f.state(); before = (f.directory() / 'state.json').read_bytes()
        state['delegate_ledger_observed'] = True
        with self.assertRaisesRegex(ao.RoomError, 'quality_evidence_integrity'):
            quality.inspect(f.directory(), state)
        self.assertEqual(pending(f.directory(), state, 'engineer'),
                         [('A distinct authorized operating instruction.', pointer['sha256'])])
        mutations = [
            lambda value: value['requests']['charter-1'].update(text='different prompt'),
            lambda value: value.update(instruction_amendments=[]),
            lambda value: value['bindings']['engineer'].update(session_id='foreign'),
            lambda value: value.update(native_outcome_source={'session_id': 'foreign'}),
            lambda value: value.update(workflow='astra_led'),
            lambda value: value.update(room_id='foreign'),
        ]
        for mutate in mutations:
            changed = copy.deepcopy(state); mutate(changed)
            with self.assertRaisesRegex(ao.RoomError, 'quality_evidence_integrity'):
                pending(f.directory(), changed, 'engineer')
        self.assertEqual((f.directory() / 'state.json').read_bytes(), before)

    def test_fresh_ledger_does_not_bypass_changed_amendment_evidence(self):
        import deepseek_adapter
        f = self.fixture
        pointer = f.service.ao_room_instruction_stage(f.room, 'retained-amendment',
            'A distinct authorized operating instruction.', 'Synthetic authorization')
        f.grant(); deepseek_adapter.Ledger(f.home)
        path = f.directory() / pointer['path']; raw = path.read_bytes()
        path.write_text('{}\n')
        before = (f.directory() / 'state.json').read_bytes(); posts = len(f.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, 'quality_evidence_integrity'):
            f.send('spec_review', 'fourth-charter')
        self.assertEqual(len(f.fake.posts), posts)
        self.assertEqual((f.directory() / 'state.json').read_bytes(), before)
        path.write_bytes(raw)
        self.assertEqual(f.send('spec_review', 'fourth-charter')['state'], 'submitted')
        self.assertEqual(len(f.fake.posts), posts + 1)


class BindingMessageTests(fixtures.Fixture):
    def test_unbound_engineer_retains_original_binding_refusal(self):
        self.room = self.open(); self.spec()
        before = (self.directory() / 'state.json').read_bytes(); posts = len(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, 'Bind the requested role first'):
            self.service.ao_room_send(self.room, 'engineer', 'Review.', 'unbound', purpose='spec_review')
        self.assertEqual(len(self.fake.posts), posts)
        self.assertEqual((self.directory() / 'state.json').read_bytes(), before)


class GrantedReconciliationQualityTests(unittest.TestCase):
    def setUp(self):
        self.f = extension_fixtures.ReviewExtensionFixture('runTest')
        self.addCleanup(self.f.doCleanups); self.f.setUp()
        f = self.f
        f.grant()
        self.amendment = f.service.ao_room_instruction_stage(f.room, 'fourth-instruction',
            'Inspect the exact new charter.', 'Synthetic user-approved amendment')
        f.send('spec_review', 'fourth-charter')
        f.fake.finish('engineer', '')
        snapshot = f.fake.snapshots['engineer']; snapshot['history_truncated'] = True
        with patch('ao_history_reconciliation.requires_strict_history', return_value=False):
            try:
                f.service.ao_room_sync(f.room)
            except quality.EvidenceError:
                pass  # Revision 3 fails its summary after preserving the real completed receipt.
        snapshot['history_truncated'] = False
        self.request = f.state()['requests']['fourth-charter']
        self.assertEqual(self.request['state'], 'completed')
        self.receipt = f.directory() / self.request['receipt']; self.receipt_bytes = self.receipt.read_bytes()
        self.assertIs(json.loads(self.receipt_bytes)['history_truncated'], True)
        self.transcript = f.root / 'synthetic-native-owner.jsonl'
        stamp = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat()
        events = [
            {'type': 'user', 'uuid': 'fourth-caller', 'sessionId': 'synthetic-native-owner',
             'timestamp': stamp(self.request['created_at'] + 0.1), 'origin': {'kind': 'human'},
             'message': {'content': self.request['text']}},
            {'type': 'assistant', 'uuid': 'fourth-quota', 'sessionId': 'synthetic-native-owner',
             'timestamp': stamp(self.request['created_at'] + 0.2), 'isApiErrorMessage': True,
             'error': 'rate_limit', 'apiErrorStatus': 429, 'message': {'model': '<synthetic>'}},
        ]
        self.transcript.write_text(''.join(json.dumps(event) + '\n' for event in events))

    def audit(self):
        f = self.f
        return f.service.ao_room_outcome_audit(f.room, ao_database_path=str(f.database),
                                              native_transcript_path=str(self.transcript))

    def status(self):
        return self.f.service.ao_room_status(self.f.room)

    def test_granted_truncated_receipt_can_run_its_first_explicit_audit(self):
        from ao_history_reconciliation import PROOF
        f = self.f; posts = len(f.fake.posts)
        self.assertNotIn(PROOF, self.request)
        result = self.audit()
        self.assertTrue(result['resume_eligible'])
        self.assertEqual(result['outcome']['kind'], 'quota_limit')
        status = self.status()
        self.assertEqual(status['engineer_context']['quality_first_review']['delivery'], 'verified_delivered')
        self.assertEqual(status['spec_review_extension']['remaining_spec_reviews'], 0)
        with self.assertRaises(ao.RoomError): f.send('spec_review', 'unapproved-fifth')
        self.assertEqual(len(f.fake.posts), posts)
        self.assertEqual(self.receipt.read_bytes(), self.receipt_bytes)

    def test_granted_missing_proof_status_is_closed_readonly_and_send_stays_strict(self):
        f = self.f
        before = {str(p.relative_to(f.directory())): p.read_bytes() for p in f.directory().rglob('*') if p.is_file()}
        result = self.status()['engineer_context']['quality_first_review']
        self.assertEqual(result['delivery'], 'unavailable_integrity')
        self.assertEqual(result['reasons'], ['quality_evidence_integrity'])
        self.assertEqual(before, {str(p.relative_to(f.directory())): p.read_bytes()
                                  for p in f.directory().rglob('*') if p.is_file()})
        posts = len(f.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, 'quality_evidence_integrity'):
            f.send('spec_review', 'no-proof-send')
        self.assertEqual(len(f.fake.posts), posts)
        self.assertNotIn('no-proof-send', f.state()['requests'])

    def test_granted_advanced_invalidation_head_allows_explicit_reaudit(self):
        from ao_history_reconciliation import PROOF, INVALIDATION
        f = self.f
        first = self.audit(); original = f.state()['requests']['fourth-charter'][PROOF]
        with self.assertRaisesRegex(ao.RoomError, 'Synthetic ordinary refusal'):
            with f.service.locked(f.room): raise ao.RoomError('Synthetic ordinary refusal')
        request = f.state()['requests']['fourth-charter']
        self.assertIn(INVALIDATION, request)
        self.assertEqual(request[PROOF], original)
        before = (f.directory() / 'state.json').read_bytes(); posts = len(f.fake.posts)
        self.assertEqual(self.status()['engineer_context']['quality_first_review']['delivery'], 'unavailable_integrity')
        self.assertEqual((f.directory() / 'state.json').read_bytes(), before)
        renewed = self.audit()
        self.assertTrue(renewed['resume_eligible'])
        self.assertNotEqual(renewed['outcome_sha256'], first['outcome_sha256'])
        self.assertNotEqual(f.state()['requests']['fourth-charter'][PROOF], original)
        self.assertEqual(self.status()['engineer_context']['quality_first_review']['delivery'], 'verified_delivered')
        self.assertEqual(len(f.fake.posts), posts)
        self.assertEqual(self.receipt.read_bytes(), self.receipt_bytes)

    def test_unavailable_history_does_not_hide_actual_amendment_corruption(self):
        f = self.f; path = f.directory() / self.amendment['path']; raw = path.read_bytes()
        path.write_text('{}\n')
        before = (f.directory() / 'state.json').read_bytes(); posts = len(f.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, 'quality_evidence_integrity'): self.audit()
        self.assertEqual((f.directory() / 'state.json').read_bytes(), before)
        self.assertEqual(len(f.fake.posts), posts)
        path.write_bytes(raw)
        self.assertTrue(self.audit()['resume_eligible'])


class StructuralAmendmentQualityTests(unittest.TestCase):
    def test_uncertain_post_grant_amendment_keeps_status_and_sync_available(self):
        from ao_instruction_amendments import pending
        f = extension_fixtures.ReviewExtensionFixture('runTest')
        self.addCleanup(f.doCleanups); f.setUp(); f.grant()
        amendment = f.service.ao_room_instruction_stage(f.room, 'uncertain-instruction',
            'Inspect this newly authorized detail.', 'Synthetic user authorization')
        f.fake.lose_ack = True; posts = len(f.fake.posts)
        self.assertEqual(f.send('spec_review', 'fourth-charter')['state'], 'uncertain')
        f.service.ao_room_status(f.room)
        self.assertEqual(pending(f.directory(), f.state(), 'engineer'),
                         [('Inspect this newly authorized detail.', amendment['sha256'])])
        status = f.finish_fourth()
        self.assertTrue(status['agreement']['agreed'])
        self.assertEqual(pending(f.directory(), f.state(), 'engineer'), [])
        self.assertEqual(len(f.fake.posts), posts + 1)

    def test_structural_equivalence_still_requires_its_exact_direct_root_receipt(self):
        from ao_instruction_amendments import pending
        f = MigrationTests('runTest')
        self.addCleanup(f.doCleanups); f.setUp(); f.agree(); f.implement()
        f.stage('later-identical'); f.continue_request('equivalent'); f.finish()
        self.assertEqual(pending(f.directory(), f.state(), 'engineer'), [])
        request = f.state()['requests']['spec_review']; path = f.directory() / request['receipt']
        raw = path.read_bytes(); path.unlink()
        with self.assertRaisesRegex(ao.RoomError, 'quality_evidence_unavailable'):
            pending(f.directory(), f.state(), 'engineer')
        path.write_bytes(raw)
        self.assertEqual(pending(f.directory(), f.state(), 'engineer'), [])

    def test_unrelated_post_grant_receipt_damage_keeps_status_closed(self):
        f = extension_fixtures.ReviewExtensionFixture('runTest')
        self.addCleanup(f.doCleanups); f.setUp(); f.grant()
        f.send('spec_review', 'fourth-charter'); f.finish_fourth()
        request = f.state()['requests']['fourth-charter']
        self.assertNotIn('instruction_amendments', request['carried'])
        self.assertNotIn(quality.EQUIVALENCE, request['carried'])
        path = f.directory() / request['receipt']; raw = path.read_bytes()
        for mode, reason in (('missing', 'quality_evidence_unavailable'), ('modified', 'quality_evidence_integrity')):
            with self.subTest(mode=mode):
                if mode == 'missing': path.unlink()
                else: path.write_text('{}\n')
                before = {str(p.relative_to(f.directory())): p.read_bytes() for p in f.directory().rglob('*') if p.is_file()}
                status = f.service.ao_room_status(f.room)['engineer_context']['quality_first_review']
                self.assertEqual(status['delivery'], 'unavailable_integrity')
                self.assertEqual(status['reasons'], [reason])
                self.assertEqual(before, {str(p.relative_to(f.directory())): p.read_bytes()
                                          for p in f.directory().rglob('*') if p.is_file()})
                path.write_bytes(raw)


class ReaderBoundaries(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def test_actual_repeated_reads_count_against_aggregate_budget(self):
        raw = b'{"value": 1}'
        (self.root / 'one.json').write_bytes(raw)
        with patch.object(quality, 'MAX_TOTAL_BYTES', len(raw) * 2):
            reader = quality._Reader(self.root)
            self.assertEqual(reader.read('one.json'), {'value': 1})
            self.assertEqual(reader.read('one.json'), {'value': 1})
            with self.assertRaisesRegex(ao.RoomError, 'quality_evidence_limit'): reader.read('one.json')
            self.assertEqual(reader.total, len(raw) * 2)

    def test_raw_descriptor_never_advances_beyond_original_extent_or_budget(self):
        path = self.root / 'growing.json'; path.write_bytes(b'{}')
        inode = path.stat().st_ino
        real_stat, real_open = os.fstat, os.fdopen
        grew, advanced = [], []
        def grow_after_admission(fd):
            info = real_stat(fd)
            if info.st_ino == inode and not grew:
                grew.append(True)
                with path.open('ab') as stream: stream.write(b' ' * 126)
            return info
        @contextlib.contextmanager
        def observe_descriptor(fd, *args, **kwargs):
            with real_open(fd, *args, **kwargs) as stream:
                try:
                    yield stream
                finally:
                    advanced.append(os.lseek(fd, 0, os.SEEK_CUR))
        with (patch.object(quality, 'MAX_TOTAL_BYTES', 2),
              patch.object(quality.os, 'fstat', side_effect=grow_after_admission),
              patch.object(quality.os, 'fdopen', side_effect=observe_descriptor)):
            reader = quality._Reader(self.root)
            with self.assertRaises(quality.EvidenceError): reader.read('growing.json')
        self.assertEqual(reader.total, 2)
        self.assertEqual(advanced, [2], 'Physical descriptor reads must obey the admitted extent and byte budget')

    def test_file_limit_boundary_fifo_symlink_and_owner(self):
        path = self.root / 'limit.json'; path.write_bytes(b'{}' + b' ' * (8388608 - 2))
        self.assertEqual(quality._Reader(self.root).read('limit.json'), {})
        with path.open('ab') as stream: stream.write(b' ')
        with self.assertRaisesRegex(ao.RoomError, 'quality_evidence_limit'):
            quality._Reader(self.root).read('limit.json')
        fifo = self.root / 'fifo.json'; os.mkfifo(fifo)
        with self.assertRaisesRegex(ao.RoomError, 'quality_evidence_integrity'):
            quality._Reader(self.root).read('fifo.json')
        path.write_bytes(b'{}')
        with patch.object(quality.os, 'getuid', return_value=-1):
            with self.assertRaisesRegex(ao.RoomError, 'quality_evidence_integrity'):
                quality._Reader(self.root).read('limit.json')


if __name__ == '__main__':
    unittest.main()
