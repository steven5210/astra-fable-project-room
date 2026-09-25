"""Behavioral matrix probes for the acceptance-review continuation lane.

This file imports only AcceptanceContinuationFixture so the existing test classes
are not collected twice.  All probes use synthetic offline state only.
"""
import copy
import json
import os
import re
import shutil
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

import ao_acceptance_extension as extension
import ao_project_room as ao
from test_ao_acceptance_extension import AcceptanceContinuationFixture


class MatrixFixture(AcceptanceContinuationFixture):
    """Shared helpers; every test still starts from the complete base fixture."""

    maxDiff = None

    def lane_root(self):
        return self.directory() / extension.BASE

    def journal_dir(self):
        return self.lane_root() / 'journal'

    def audits_dir(self):
        return self.lane_root() / 'audits'

    def lane_snapshot(self):
        base = self.lane_root()
        result = {}
        if not base.exists() and not base.is_symlink():
            return result
        for root, dirs, files in os.walk(base, followlinks=False):
            for name in dirs + files:
                path = Path(root) / name
                rel = str(path.relative_to(base))
                if path.is_symlink():
                    result[rel] = b'symlink:' + os.readlink(path).encode()
                elif path.is_dir():
                    result[rel] = b'dir'
                elif path.is_file():
                    result[rel] = path.read_bytes()
        return result

    def journal_bytes(self):
        if not self.journal_dir().exists():
            return {}
        return {name: (self.journal_dir() / name).read_bytes()
                for name in sorted(os.listdir(self.journal_dir()))
                if (self.journal_dir() / name).is_file()}

    def restore_journal(self, saved):
        directory = self.journal_dir()
        if directory.exists():
            for name in list(os.listdir(directory)):
                path = directory / name
                if path.is_file() or path.is_symlink():
                    path.unlink()
        else:
            directory.mkdir(parents=True, exist_ok=True)
        for name, data in saved.items():
            (directory / name).write_bytes(data)

    def remove_lane(self):
        base = self.lane_root()
        if base.is_symlink() or base.is_file():
            base.unlink()
        elif base.exists():
            shutil.rmtree(base)

    def state_bytes(self):
        return (self.directory() / 'state.json').read_bytes()

    def write_state(self, state):
        ao.atomic(self.directory() / 'state.json', state)

    def write_state_bytes(self, data):
        (self.directory() / 'state.json').write_bytes(data)

    def sql(self, statement, params=()):
        with sqlite3.connect(self.database) as database:
            database.execute(statement, params)

    def assert_refused_clean(self, operation, regex=None, expected_state=None):
        before = self.lane_snapshot()
        posts = len(self.fake.posts)
        if regex is None:
            context = self.assertRaises(ao.RoomError)
        else:
            context = self.assertRaisesRegex(ao.RoomError, regex)
        with context:
            operation()
        self.assertEqual(len(self.fake.posts), posts)
        self.assertEqual(self.lane_snapshot(), before)
        if expected_state is not None:
            summary = extension.summary(self.service, self.state())
            self.assertIsNotNone(summary)
            self.assertEqual(summary['state'], expected_state)

    def assert_inconsistent_refusals(self, next_review, accept_id):
        block = self.service.ao_room_status(self.room)['acceptance_review_extension']
        self.assertEqual(block['state'], 'inconsistent')
        self.assertNotIn(block['state'], ('consumed', 'unconsumed'))
        posts = len(self.fake.posts)
        for operation in (lambda: self.service.ao_room_sync(self.room),
                          lambda: self.audit(next_review),
                          lambda: self.service.ao_room_accept(self.room, accept_id)):
            with self.subTest(operation=operation), self.assertRaises(ao.RoomError):
                operation()
            self.assertEqual(len(self.fake.posts), posts)

    def consumed_fourth(self, decision='rejected'):
        inputs, _ = self.grant()
        self.send('acceptance_review', 'fourth')
        self.finish_review('fourth', decision)
        return inputs

    def room_files(self):
        result = {}
        for path in self.directory().rglob('*'):
            if path.is_file() and not path.is_symlink() and path.name != 'state.json':
                result[str(path.relative_to(self.directory()))] = path.read_bytes()
        return result

    def assert_preserved(self, snapshot):
        for relative, data in snapshot.items():
            path = self.directory() / relative
            self.assertTrue(path.is_file(), relative)
            self.assertEqual(path.read_bytes(), data, relative)


class AcceptanceMatrixTests(MatrixFixture):
    """P1-P9 and P11-P16 on the complete base fixture."""

    def test_p1_unfinished_verification(self):
        def add_running():
            state = self.state()
            state['verifications'].append({'id': 'synthetic-running-verification',
                                           'state': 'running', 'candidate_sha256': '0' * 64})
            self.write_state(state)

        original = self.state_bytes()
        try:
            add_running()
            self.assert_refused_clean(self.audit, regex='An unfinished verification is recorded')
            self.assertFalse(self.lane_root().exists())
        finally:
            self.write_state_bytes(original)

        inputs = self.grant_inputs()
        original = self.state_bytes()
        try:
            add_running()
            self.assert_refused_clean(lambda: self.service.ao_room_acceptance_review_extend(**inputs),
                                      regex='An unfinished verification is recorded')
            self.assertIsNone(self.state().get(extension.KEY))
            self.assertFalse(self.journal_dir().exists() and os.listdir(self.journal_dir()))
        finally:
            self.write_state_bytes(original)

        self.service.ao_room_acceptance_review_extend(**inputs)
        original = self.state_bytes()
        try:
            add_running()
            # Journal integrity notices the changed pinned history before settled().
            self.assert_refused_clean(lambda: self.send('acceptance_review', 'fourth'),
                                      regex='acceptance or verification history changed while its grant is unused',
                                      expected_state='inconsistent')
        finally:
            self.write_state_bytes(original)
        posts = len(self.fake.posts)
        self.send('acceptance_review', 'fourth')
        self.assertEqual(len(self.fake.posts), posts + 1)
        self.assertEqual(extension.summary(self.service, self.state())['state'], 'consumed')

    def test_p2_uncertain_fourth(self):
        inputs, _ = self.grant()
        initial = self.lane_snapshot()
        self.fake.lose_ack = True
        first = self.send('acceptance_review', 'fourth')
        self.assertEqual(first['state'], 'uncertain')
        self.assertEqual(self.state()['requests']['fourth']['state'], 'uncertain')
        after = self.lane_snapshot()
        self.assertNotEqual(initial, after)
        posts = len(self.fake.posts)
        audits = sorted(os.listdir(self.audits_dir()))
        with self.assertRaisesRegex(ao.RoomError, 'active or uncertain'):
            self.audit('fifth')
        self.assertEqual(sorted(os.listdir(self.audits_dir())), audits)
        with self.assertRaisesRegex(ao.RoomError, 'active or uncertain'):
            self.service.ao_room_acceptance_review_extend(
                **{**inputs, 'request_id': 'grant-fifth'})
        self.assertEqual(sorted(os.listdir(self.audits_dir())), audits)
        self.assertEqual(self.lane_snapshot(), after)
        self.assertEqual(self.send('acceptance_review', 'fourth'), first)
        self.assertEqual(len(self.fake.posts), posts)
        with self.assertRaisesRegex(ao.RoomError, 'already belongs to another payload'):
            self.service.ao_room_send(self.room, 'reviewer', 'Changed caller bytes.', 'fourth',
                                      purpose='acceptance_review')
        self.assertEqual(len(self.fake.posts), posts)
        self.assertEqual(self.lane_snapshot(), after)

        request = self.state()['requests']['fourth']
        self.fake.finish('reviewer', json.dumps({**request['review'],
                                                 'decision': 'rejected',
                                                 'review': 'Synthetic independent verdict.'}))
        self.service.ao_room_sync(self.room)
        self.assertEqual(self.state()['requests']['fourth']['state'], 'completed')
        self.assertEqual(self.lane_snapshot(), after)
        self.assertEqual(extension.summary(self.service, self.state())['state'], 'consumed')

        audit = self.audit('fifth')
        self.assertTrue(audit['eligible'])
        result = self.service.ao_room_acceptance_review_extend(
            self.room, audit['audit_sha256'], 'grant-fifth',
            'Actual synthetic user approval for needed additional reviews.',
            'synthetic-user-message',
            'A necessary further independent review in the same scope.')
        self.assertEqual(result['review_request_id'], 'fifth')
        self.assertEqual(result['remaining_additional_reviews'], 1)

    def test_p3_reordered_records(self):
        self.grant()
        self.send('acceptance_review', 'fourth')
        self.finish_review('fourth', 'rejected')
        state_bytes = self.state_bytes()
        journal = self.journal_dir()
        one = (journal / '000001.json').read_bytes()
        two = (journal / '000002.json').read_bytes()
        try:
            (journal / '000001.json').write_bytes(two)
            (journal / '000002.json').write_bytes(one)
            self.assert_inconsistent_refusals('fifth', 'fourth')
        finally:
            (journal / '000001.json').write_bytes(one)
            (journal / '000002.json').write_bytes(two)
        self.write_state_bytes(state_bytes)

        state = self.state()
        try:
            state[extension.KEY]['entries'].reverse()
            self.write_state(state)
            self.assert_inconsistent_refusals('fifth', 'fourth')
        finally:
            self.write_state_bytes(state_bytes)

    def test_p4_orphan_and_unclaimed_records_consumed(self):
        self.grant()
        self.send('acceptance_review', 'fourth')
        self.finish_review('fourth', 'rejected')
        original = self.journal_bytes()
        original_state = self.state_bytes()
        mutations = [
            ('trailing_copy_of_grant',
             lambda: (self.journal_dir() / '000003.json').write_bytes((self.journal_dir() / '000001.json').read_bytes())),
            ('two_trailing',
             lambda: [(self.journal_dir() / '000003.json').write_bytes((self.journal_dir() / '000001.json').read_bytes()),
                      (self.journal_dir() / '000004.json').write_bytes((self.journal_dir() / '000001.json').read_bytes())]),
            ('trailing_garbage',
             lambda: (self.journal_dir() / '000003.json').write_bytes(b'{not json')),
            ('odd_name',
             lambda: (self.journal_dir() / 'unexpected.bak').write_bytes(b'{}')),
            ('sequence_gap',
             lambda: (self.journal_dir() / '000002.json').rename(self.journal_dir() / '000003.json')),
        ]
        for name, mutate in mutations:
            with self.subTest(case=name):
                try:
                    mutate()
                    block = self.service.ao_room_status(self.room)['acceptance_review_extension']
                    self.assertEqual(block['state'], 'inconsistent')
                    self.assertNotIn(block['state'], ('consumed', 'unconsumed'))
                    posts = len(self.fake.posts)
                    with self.assertRaises(ao.RoomError):
                        self.service.ao_room_sync(self.room)
                    with self.assertRaises(ao.RoomError):
                        self.audit('fifth')
                    with self.assertRaises(ao.RoomError):
                        self.service.ao_room_acceptance_review_extend(
                            self.room, 'a' * 64, 'grant-extra',
                            'Actual synthetic user approval for needed additional reviews.',
                            'synthetic-user-message',
                            'A necessary further independent review in the same scope.')
                    with self.assertRaises(ao.RoomError):
                        self.send('acceptance_review', 'fifth')
                    self.assertEqual(len(self.fake.posts), posts)
                    self.assertFalse(extension.summary(self.service, self.state()) is None)
                finally:
                    self.restore_journal(original)
                    self.write_state_bytes(original_state)

    def test_p4_orphan_and_unclaimed_records_unused_grant(self):
        inputs, _ = self.grant()
        original = self.journal_bytes()
        original_state = self.state_bytes()
        mutations = [
            ('trailing_copy_of_grant',
             lambda: (self.journal_dir() / '000002.json').write_bytes((self.journal_dir() / '000001.json').read_bytes())),
            ('two_trailing',
             lambda: [(self.journal_dir() / '000002.json').write_bytes((self.journal_dir() / '000001.json').read_bytes()),
                      (self.journal_dir() / '000003.json').write_bytes((self.journal_dir() / '000001.json').read_bytes())]),
            ('trailing_garbage',
             lambda: (self.journal_dir() / '000002.json').write_bytes(b'{not json')),
            ('odd_name',
             lambda: (self.journal_dir() / 'unexpected.bak').write_bytes(b'{}')),
            ('sequence_gap',
             lambda: (self.journal_dir() / '000001.json').rename(self.journal_dir() / '000002.json')),
        ]
        for name, mutate in mutations:
            with self.subTest(case=name):
                try:
                    mutate()
                    block = self.service.ao_room_status(self.room)['acceptance_review_extension']
                    self.assertEqual(block['state'], 'inconsistent')
                    posts = len(self.fake.posts)
                    with self.assertRaises(ao.RoomError):
                        self.service.ao_room_sync(self.room)
                    with self.assertRaises(ao.RoomError):
                        self.service.ao_room_acceptance_review_extend(
                            self.room, inputs['audit_sha256'], 'grant-extra',
                            'Actual synthetic user approval for needed additional reviews.',
                            'synthetic-user-message',
                            'A necessary further independent review in the same scope.')
                    with self.assertRaises(ao.RoomError):
                        self.send('acceptance_review', 'fourth')
                    self.assertEqual(len(self.fake.posts), posts)
                finally:
                    self.restore_journal(original)
                    self.write_state_bytes(original_state)

    def test_p5_bounded_utf8_evidence(self):
        inputs = self.grant_inputs()
        inputs['authorization'] = '批准：后续需要的独立评审 ✦ — ünïcødé ✓'
        inputs['authorization_reference'] = '用户消息 #4 — 引用 “scope” · ✓'
        inputs['diagnosis'] = '诊断：需要进一步独立验收审查 — データ ✓'
        first = self.service.ao_room_acceptance_review_extend(**inputs)
        raw = (self.journal_dir() / '000001.json').read_bytes()
        raw.decode('utf-8')
        entry = json.loads(raw)
        self.assertEqual(entry['inputs']['authorization'], inputs['authorization'])
        self.assertEqual(entry['inputs']['authorization_reference'], inputs['authorization_reference'])
        self.assertEqual(entry['inputs']['diagnosis'], inputs['diagnosis'])
        second = self.service.ao_room_acceptance_review_extend(**inputs)
        self.assertEqual(second['receipt_sha256'], first['receipt_sha256'])
        self.assertEqual(len(os.listdir(self.journal_dir())), 1)
        self.assertEqual((self.journal_dir() / '000001.json').read_bytes(), raw)

        before_lane = self.lane_snapshot()
        before_state = self.state_bytes()
        posts = len(self.fake.posts)
        for kwargs, regex in (
                (dict(authorization='é' * 5000), 'at most 8192 UTF-8 bytes'),
                (dict(authorization_reference='é' * 2000), 'at most 2048 UTF-8 bytes'),
                (dict(diagnosis='é' * 5000), 'at most 8192 UTF-8 bytes')):
            with self.subTest(field=next(iter(kwargs))):
                values = dict(room_id=self.room, audit_sha256='a' * 64, request_id='grant-bound',
                              authorization='Approved.', authorization_reference='reference.',
                              diagnosis='diagnosis.')
                values.update(kwargs)
                with self.assertRaisesRegex(ao.RoomError, regex):
                    self.service.ao_room_acceptance_review_extend(**values)
                self.assertEqual(self.lane_snapshot(), before_lane)
                self.assertEqual(self.state_bytes(), before_state)
                self.assertEqual(len(self.fake.posts), posts)
        with self.assertRaisesRegex(ao.RoomError, 'at most 160 UTF-8 bytes'):
            self.service.ao_room_acceptance_review_audit(
                self.room, 'fourth', ao.digest(self.message.encode()), str(self.database),
                'x' * 161, 'synthetic-native-reviewer')
        self.assertEqual(self.lane_snapshot(), before_lane)
        self.assertEqual(self.state_bytes(), before_state)
        self.assertEqual(len(self.fake.posts), posts)

    def test_p5c_audit_record_size_bound(self):
        audit = self.audit('fourth')
        audit_path = self.audits_dir() / (audit['audit_sha256'] + '.json')
        audit_size = audit_path.stat().st_size
        record = json.loads(audit_path.read_text(encoding='utf-8'))
        max_manifest = max((self.directory() / path).stat().st_size
                           for path in record['evidence']['manifest'])
        bound = max_manifest + 1
        self.assertGreater(audit_size, bound)
        before = sorted(os.listdir(self.audits_dir()))
        with patch.object(extension, 'MAX_RECORD_BYTES', bound):
            with self.assertRaisesRegex(ao.RoomError,
                                        'complete acceptance-review audit exceeds its readable size bound'):
                self.audit('fifth')
        self.assertEqual(sorted(os.listdir(self.audits_dir())), before)

    def test_p5c_extend_record_size_bound(self):
        audit = self.audit('fourth')
        audit_path = self.audits_dir() / (audit['audit_sha256'] + '.json')
        record = json.loads(audit_path.read_text(encoding='utf-8'))
        max_manifest = max((self.directory() / path).stat().st_size
                           for path in record['evidence']['manifest'])
        bound = max_manifest + 1
        with patch.object(extension, 'MAX_RECORD_BYTES', bound):
            with self.assertRaisesRegex(ao.RoomError,
                                        'continuation journal entry exceeds its readable size bound'):
                self.service.ao_room_acceptance_review_extend(
                    self.room, audit['audit_sha256'], 'grant-bounded',
                    'A' * 8000, 'é' * 1000, '診' * 2700)
        self.assertFalse(self.journal_dir().exists() and os.listdir(self.journal_dir()))
        self.assertIsNone(self.state().get(extension.KEY))

    def test_p5d_status_bounded_reader(self):
        self.grant()
        record_size = (self.journal_dir() / '000001.json').stat().st_size
        with patch.object(extension, 'MAX_RECORD_BYTES', max(1, record_size - 1)):
            block = self.service.ao_room_status(self.room)['acceptance_review_extension']
            self.assertEqual(block['state'], 'inconsistent')
            posts = len(self.fake.posts)
            with self.assertRaises(ao.RoomError):
                self.service.ao_room_sync(self.room)
            self.assertEqual(len(self.fake.posts), posts)
        self.assertEqual(extension.summary(self.service, self.state())['state'], 'unconsumed')

    def test_p5e_invalid_utf8_record(self):
        self.grant()
        path = self.journal_dir() / '000001.json'
        original = path.read_bytes()
        try:
            path.write_bytes(b'\xff\xfe{not utf8')
            self.assertEqual(self.service.ao_room_status(self.room)['acceptance_review_extension']['state'],
                             'inconsistent')
            with self.assertRaises(ao.RoomError):
                self.service.ao_room_sync(self.room)
        finally:
            path.write_bytes(original)
        self.assertEqual(extension.summary(self.service, self.state())['state'], 'unconsumed')

    def test_p5f_invalid_digest_and_identifier(self):
        with self.assertRaisesRegex(ao.RoomError, 'Use the exact intended reviewer message SHA256'):
            self.service.ao_room_acceptance_review_audit(
                self.room, 'fourth', 'A' * 64, str(self.database),
                'synthetic-native-engineer', 'synthetic-native-reviewer')
        with self.assertRaisesRegex(ao.RoomError, 'Invalid identifier'):
            self.service.ao_room_acceptance_review_audit(
                self.room, 'bad id!', ao.digest(self.message.encode()), str(self.database),
                'synthetic-native-engineer', 'synthetic-native-reviewer')
        self.assertFalse((self.directory() / extension.BASE).exists())

    def test_p6_symlinked_and_unsafe_paths(self):
        outside = self.root / 'outside-lane'
        outside.mkdir()
        base = self.directory() / extension.BASE
        try:
            base.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ao.RoomError, 'symlinked or replaced storage refuses'):
                self.audit()
            self.assertEqual(list(outside.iterdir()), [])
        finally:
            if base.is_symlink():
                base.unlink()
            shutil.rmtree(outside, ignore_errors=True)

        base.mkdir()
        try:
            (base / 'audits').symlink_to(self.root / 'outside-audits', target_is_directory=True)
            (self.root / 'outside-audits').mkdir()
            with self.assertRaisesRegex(ao.RoomError, 'symlinked or replaced storage refuses'):
                self.audit()
            self.assertEqual(list((self.root / 'outside-audits').iterdir()), [])
        finally:
            if (base / 'audits').is_symlink():
                (base / 'audits').unlink()
        self.remove_lane()

        audit = self.audit('fourth')
        outside_journal = self.root / 'outside-journal'
        outside_journal.mkdir()
        try:
            (base / 'journal').symlink_to(outside_journal, target_is_directory=True)
            with self.assertRaisesRegex(ao.RoomError, 'symlinked or replaced storage refuses'):
                self.service.ao_room_acceptance_review_extend(
                    self.room, audit['audit_sha256'], 'grant-journal-link',
                    'Approved.', 'reference.', 'diagnosis.')
            self.assertEqual(list(outside_journal.iterdir()), [])
        finally:
            if (base / 'journal').is_symlink():
                (base / 'journal').unlink()
            shutil.rmtree(outside_journal, ignore_errors=True)
        self.remove_lane()

    def assert_audit_file_refused_and_restored(self, mutation, message):
        audit = self.audit('fourth')
        audit_path = self.audits_dir() / (audit['audit_sha256'] + '.json')
        original = audit_path.read_bytes()
        inputs = dict(room_id=self.room, audit_sha256=audit['audit_sha256'], request_id='grant-audit-path',
                      authorization='Approved.', authorization_reference='reference.', diagnosis='diagnosis.')
        posts = len(self.fake.posts)
        state = self.state_bytes()
        try:
            if mutation == 'missing':
                audit_path.unlink()
            elif mutation == 'modified':
                audit_path.write_bytes(original + b'\n')
            else:
                copy_path = self.root / 'audit-copy.json'
                copy_path.write_bytes(original)
                audit_path.unlink()
                audit_path.symlink_to(copy_path)
            with self.assertRaisesRegex(ao.RoomError, message):
                self.service.ao_room_acceptance_review_extend(**inputs)
            self.assertFalse(self.journal_dir().exists() and os.listdir(self.journal_dir()))
            self.assertIsNone(self.state().get(extension.KEY))
            self.assertEqual(self.state_bytes(), state)
            self.assertEqual(len(self.fake.posts), posts)
        finally:
            if audit_path.is_symlink():
                audit_path.unlink()
            audit_path.write_bytes(original)
        granted = self.service.ao_room_acceptance_review_extend(**inputs)
        self.assertEqual(granted['remaining_additional_reviews'], 1)
        self.assertEqual(audit_path.read_bytes(), original)
        self.assertEqual(len(self.fake.posts), posts)

    def test_p6_audit_file_missing(self):
        self.assert_audit_file_refused_and_restored(
            'missing', 'Acceptance-review continuation evidence is unreadable or inconsistent')

    def test_p6_audit_file_modified(self):
        self.assert_audit_file_refused_and_restored(
            'modified', 'Acceptance-review continuation evidence was modified')

    def test_p6_audit_file_symlinked(self):
        self.assert_audit_file_refused_and_restored(
            'symlink', 'Acceptance-review continuation evidence is unreadable or inconsistent')

    def test_p6_deleted_grant_audit_restored(self):
        self.grant()
        audit_path = self.audits_dir() / (json.loads((self.journal_dir() / '000001.json').read_text())
                                          ['inputs']['audit_sha256'] + '.json')
        original = audit_path.read_bytes()
        try:
            audit_path.unlink()
            self.assertEqual(self.service.ao_room_status(self.room)['acceptance_review_extension']['state'],
                             'inconsistent')
            posts = len(self.fake.posts)
            with self.assertRaises(ao.RoomError):
                self.send('acceptance_review', 'fourth')
            self.assertEqual(len(self.fake.posts), posts)
        finally:
            audit_path.write_bytes(original)
        posts = len(self.fake.posts)
        self.send('acceptance_review', 'fourth')
        self.assertEqual(len(self.fake.posts), posts + 1)

    def test_p6_owner_database_unsafe_inputs(self):
        for name, path in (('missing', self.root / 'missing-owner.db'),
                           ('directory', self.root / 'owner-dir'),
                           ('empty', self.root / 'empty-owner.db')):
            with self.subTest(case=name):
                if name == 'directory':
                    path.mkdir()
                elif name == 'empty':
                    path.write_bytes(b'')
                before = sorted(os.listdir(self.directory()))
                with self.assertRaisesRegex(ao.RoomError, 'native engineer owner evidence is unavailable or contradictory'):
                    self.service.ao_room_acceptance_review_audit(
                        self.room, 'fourth', ao.digest(self.message.encode()), str(path),
                        'synthetic-native-engineer', 'synthetic-native-reviewer')
                self.assertFalse((self.directory() / extension.BASE).exists())
                self.assertEqual(sorted(os.listdir(self.directory())), before)

    def test_p6_replaced_storage_paths(self):
        base = self.directory() / extension.BASE
        try:
            base.write_bytes(b'not a directory')
            with self.assertRaisesRegex(ao.RoomError, 'symlinked or replaced storage refuses'):
                self.audit()
        finally:
            base.unlink()
        self.remove_lane()

        base.mkdir()
        try:
            (base / 'journal').write_bytes(b'not a directory')
            with self.assertRaisesRegex(ao.RoomError, 'symlinked or replaced storage refuses'):
                self.audit()
        finally:
            (base / 'journal').unlink()
        self.remove_lane()

        base.mkdir()
        try:
            (base / 'audits').write_bytes(b'not a directory')
            with self.assertRaisesRegex(ao.RoomError, 'symlinked or replaced storage refuses'):
                self.audit()
        finally:
            (base / 'audits').unlink()
        self.remove_lane()

    def test_p7_whole_chain_retention_and_reuse(self):
        snapshots = [self.room_files()]
        inputs, _ = self.grant()
        snapshots.append(self.room_files())
        self.assert_preserved(snapshots[0])

        self.send('acceptance_review', 'fourth')
        self.finish_review('fourth', 'rejected')
        snapshots.append(self.room_files())
        self.assert_preserved(snapshots[0])
        self.assert_preserved(snapshots[1])
        posts = len(self.fake.posts)
        with self.assertRaises(ao.RoomError):
            self.service.ao_room_accept(self.room, 'fourth')
        with self.assertRaisesRegex(ao.RoomError, 'Every audited acceptance-review grant is consumed'):
            self.send('acceptance_review', 'different-review')
        self.assertEqual(len(self.fake.posts), posts)

        orders_after_fourth = [r['created_order'] for r in sorted(
            (r for r in self.state()['requests'].values() if r['role'] == 'reviewer'),
            key=lambda r: r['created_order'])]

        audit5 = self.audit('fifth')
        fifth = self.service.ao_room_acceptance_review_extend(
            self.room, audit5['audit_sha256'], 'grant-fifth',
            inputs['authorization'], inputs['authorization_reference'],
            'A necessary further independent review in the same scope.')
        self.assertEqual(fifth['review_request_id'], 'fifth')
        snapshots.append(self.room_files())
        self.assert_preserved(snapshots[0])
        self.assert_preserved(snapshots[1])
        self.assert_preserved(snapshots[2])

        posts = len(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, 'Only the exact reviewer request identity named'):
            self.send('acceptance_review', 'alternate-review')
        self.assertEqual(len(self.fake.posts), posts)
        self.send('acceptance_review', 'fifth')
        self.finish_review('fifth', 'approved')
        self.assertTrue(self.service.ao_room_accept(self.room, 'fifth')['accepted'])
        snapshots.append(self.room_files())
        for snapshot in snapshots[:-1]:
            self.assert_preserved(snapshot)

        posts = len(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, 'Every audited acceptance-review grant is consumed'):
            self.send('acceptance_review', 'sixth')
        self.assertEqual(len(self.fake.posts), posts)

        state = self.state()
        reviewers = sorted((r for r in state['requests'].values() if r['role'] == 'reviewer'),
                           key=lambda r: r['created_order'])
        self.assertEqual([r['request_id'] for r in reviewers],
                         ['prior-1', 'prior-2', 'prior-3', 'fourth', 'fifth'])
        self.assertEqual([r['created_order'] for r in reviewers][:4], orders_after_fourth)
        self.assertEqual(self.service.ao_room_status(self.room)['acceptance_review_attempts'], 5)
        self.assertEqual(self.service.ao_room_status(self.room)['acceptance_review_extension']['state'],
                         'consumed')
        entries = extension.validate(self.service, self.state())['entries']
        self.assertEqual([entry['kind'] for entry in entries],
                         ['grant', 'consumption', 'grant', 'consumption'])
        first_grant = json.loads((self.journal_dir() / '000001.json').read_text())
        second_grant = json.loads((self.journal_dir() / '000003.json').read_text())
        self.assertEqual(first_grant['inputs']['authorization'], second_grant['inputs']['authorization'])
        self.assertNotEqual(first_grant['inputs']['audit_sha256'], second_grant['inputs']['audit_sha256'])
        self.assertNotEqual(first_grant['inputs']['request_id'], second_grant['inputs']['request_id'])
        self.assertNotEqual(first_grant['grant']['review_request_id'], second_grant['grant']['review_request_id'])

    def test_p8_freshness_drift_categories(self):
        inputs = self.grant_inputs()
        audit_state = self.state_bytes()
        state = self.state()
        handoff_path = self.directory() / state['handoff']
        handoff_bytes = handoff_path.read_bytes()
        engineering_receipt = self.directory() / state['requests']['implementation']['receipt']
        engineering_bytes = engineering_receipt.read_bytes()
        spec_path = self.directory() / state['spec']
        spec_bytes = spec_path.read_bytes()
        prior_receipt = self.directory() / state['requests']['prior-2']['receipt']
        prior_bytes = prior_receipt.read_bytes()
        tracked = self.repo / 'feature.txt'
        tracked_bytes = tracked.read_bytes()
        untracked = self.repo / 'drift-untracked.txt'
        reviewer_snapshot = copy.deepcopy(self.fake.snapshots['reviewer'])
        engineer_snapshot = copy.deepcopy(self.fake.snapshots['engineer'])
        database_bytes = self.database.read_bytes()
        reviewer_turn = state['requests']['prior-1']['turn_id']

        def state_case(change):
            def mutate():
                current = self.state()
                change(current)
                self.write_state(current)
            return mutate

        def snapshot_case(role, mutate):
            original = copy.deepcopy(self.fake.snapshots[role])
            def run():
                mutate(self.fake.snapshots[role])
            def restore():
                self.fake.snapshots[role] = copy.deepcopy(original)
            return run, restore

        cases = [
            ('handoff', lambda: handoff_path.write_bytes(handoff_bytes + b'\n'),
             lambda: handoff_path.write_bytes(handoff_bytes)),
            ('engineering_receipt', lambda: engineering_receipt.write_bytes(engineering_bytes + b'\n'),
             lambda: engineering_receipt.write_bytes(engineering_bytes)),
            ('spec_record', lambda: spec_path.write_bytes(spec_bytes + b'\n'),
             lambda: spec_path.write_bytes(spec_bytes)),
            ('prior_receipt', lambda: prior_receipt.write_bytes(prior_bytes + b'\n'),
             lambda: prior_receipt.write_bytes(prior_bytes)),
            ('untracked_file', lambda: untracked.write_text('drift\n'),
             lambda: untracked.unlink() if untracked.exists() else None),
            ('tracked_file', lambda: tracked.write_bytes(b'changed\n'),
             lambda: tracked.write_bytes(tracked_bytes)),
            ('reviewer_binding', state_case(lambda current: current['bindings']['reviewer'].__setitem__('reasoning_effort', 'high')),
             lambda: None),
            ('room_identity', state_case(lambda current: current.__setitem__('project_path', str(self.root / 'other-project'))),
             lambda: None),
            ('forged_acceptance', state_case(lambda current: current['acceptances'].append({'forged': True})),
             lambda: None),
            ('removed_verification', state_case(lambda current: current['verifications'].pop()),
             lambda: None),
            ('prior_reviewer_text', state_case(lambda current: current['requests']['prior-1'].__setitem__('text_sha256', '0' * 64)),
             lambda: None),
            ('spec_record_pointer', state_case(lambda current: current.__setitem__('spec_record_sha256', '0' * 64)),
             lambda: None),
        ]
        run, restore = snapshot_case('reviewer', lambda snapshot: snapshot['turns'].append(
            {'id': 'extra-reviewer-turn', 'providerTurnId': 'native-extra-reviewer-turn', 'state': 'completed'}))
        cases.append(('native_reviewer_extra_turn', run, restore))
        run, restore = snapshot_case('engineer', lambda snapshot: snapshot['turns'].append(
            {'id': 'extra-engineer-turn', 'providerTurnId': 'native-extra-engineer-turn', 'state': 'completed'}))
        cases.append(('native_engineer_extra_turn', run, restore))
        cases.append(('owner_reviewer_conversation',
                      lambda: self.sql("UPDATE conversation_branches SET provider_conversation_id='replaced' WHERE session_id='reviewer'"),
                      lambda: self.database.write_bytes(database_bytes)))
        cases.append(('owner_engineer_workspace',
                      lambda: self.sql("UPDATE sessions SET workspace_path=? WHERE id='engineer'",
                                       (str(self.root / 'other-workspace'),)),
                      lambda: self.database.write_bytes(database_bytes)))

        expected_messages = {
            'handoff': (
                'The acceptance-review audit is stale; room, candidate, native or retained evidence changed',
                'Acceptance-review retained evidence is missing or modified'),
            'engineering_receipt': (
                'The acceptance-review audit is stale; room, candidate, native or retained evidence changed',
                'Acceptance-review retained evidence is missing or modified'),
            'spec_record': (
                'The acceptance-review audit is stale; room, candidate, native or retained evidence changed',
                'Acceptance-review retained evidence is missing or modified'),
            'prior_receipt': (
                'The acceptance-review audit is stale; room, candidate, native or retained evidence changed',
                'Acceptance-review retained evidence is missing or modified'),
            'untracked_file': (
                'Candidate changed after verification; verify and review the new candidate',
                'Candidate changed after verification; verify and review the new candidate'),
            'tracked_file': (
                'Candidate changed after verification; verify and review the new candidate',
                'Candidate changed after verification; verify and review the new candidate'),
            'reviewer_binding': (
                'An additional acceptance review requires the bound native Fable engineer and Codex reviewer at max effort',
                'The unused acceptance-review grant pins bindings; it changed, so nothing may be dispatched until it is diagnosed'),
            'room_identity': (
                'The acceptance-review audit is stale; room, candidate, native or retained evidence changed',
                'Acceptance-review room, project or workflow identity changed'),
            'forged_acceptance': (
                'The acceptance-review audit is stale; room, candidate, native or retained evidence changed',
                'Acceptance-review acceptance or verification history changed while its grant is unused'),
            'removed_verification': (
                'The acceptance-review audit is stale; room, candidate, native or retained evidence changed',
                'Acceptance-review retained acceptance or verification history changed'),
            'prior_reviewer_text': (
                'Owning native request differs from its completed receipt or observed message',
                # Modern prompt integrity is checked before acceptance-grant admission.
                'A saved prompt projection is invalid; preserve it and reconcile before dispatch'),
            'spec_record_pointer': (
                'Immutable specification was modified',
                'The unused acceptance-review grant pins spec_record_sha256; it changed, so nothing may be dispatched until it is diagnosed'),
            'native_reviewer_extra_turn': (
                'Cannot attribute semantic outcome to an unchanged completed native result',
                'Cannot attribute semantic outcome to an unchanged completed native result'),
            'native_engineer_extra_turn': (
                'Cannot attribute semantic outcome to an unchanged completed native result',
                'Cannot attribute semantic outcome to an unchanged completed native result'),
            'owner_reviewer_conversation': (
                'The retained native reviewer owner evidence is unavailable or contradictory',
                'The retained native reviewer owner evidence is unavailable or contradictory'),
            'owner_engineer_workspace': (
                'The native engineer owner differs from the retained room, session, workspace or supplied native session',
                'The native engineer owner differs from the retained room, session, workspace or supplied native session'),
        }
        posts = len(self.fake.posts)

        for name, mutate, restore in cases:
            with self.subTest(phase='extend', category=name):
                try:
                    mutate()
                    with self.assertRaisesRegex(ao.RoomError, re.escape(expected_messages[name][0])):
                        self.service.ao_room_acceptance_review_extend(
                            **{**inputs, 'request_id': 'grant-drift'})
                    self.assertFalse(self.journal_dir().exists() and os.listdir(self.journal_dir()))
                    self.assertEqual(len(self.fake.posts), posts)
                    self.assertIsNone(self.state().get(extension.KEY))
                finally:
                    restore()
                    self.write_state_bytes(audit_state)
                    self.database.write_bytes(database_bytes)

        self.service.ao_room_acceptance_review_extend(**inputs)
        grant_state = self.state_bytes()
        journal_saved = self.journal_bytes()
        posts = len(self.fake.posts)
        # Status diagnoses local retained-evidence drift. Live workspace/native
        # drift is checked by dispatch; status deliberately does not attest it.
        inconsistent_categories = {
            'handoff', 'engineering_receipt', 'spec_record', 'prior_receipt',
            'reviewer_binding', 'room_identity', 'forged_acceptance',
            'removed_verification', 'prior_reviewer_text', 'spec_record_pointer',
        }
        for name, mutate, restore in cases:
            with self.subTest(phase='send', category=name):
                try:
                    mutate()
                    with self.assertRaisesRegex(ao.RoomError, re.escape(expected_messages[name][1])):
                        self.send('acceptance_review', 'fourth')
                    self.assertEqual(len(self.fake.posts), posts)
                    self.assertEqual(self.journal_bytes(), journal_saved)
                    expected = 'inconsistent' if name in inconsistent_categories else 'unconsumed'
                    self.assertEqual(extension.summary(self.service, self.state())['state'], expected)
                finally:
                    restore()
                    self.write_state_bytes(grant_state)
                    self.database.write_bytes(database_bytes)
                    self.restore_journal(journal_saved)
                self.assertEqual(extension.summary(self.service, self.state())['state'], 'unconsumed')
        self.send('acceptance_review', 'fourth')
        self.assertEqual(len(self.fake.posts), posts + 1)
        self.fake.snapshots['reviewer'] = reviewer_snapshot
        self.fake.snapshots['engineer'] = engineer_snapshot

    def test_p9_wrong_role_and_never_repurposed_identities(self):
        inputs, _ = self.grant()
        posts = len(self.fake.posts)
        journal = self.journal_bytes()
        with self.assertRaisesRegex(ao.RoomError, 'unused acceptance-review grant freezes'):
            self.service.ao_room_send(self.room, 'engineer', 'Engineer work.', 'fourth',
                                      purpose='correction')
        with self.assertRaisesRegex(ao.RoomError, 'grant identity'):
            self.service.ao_room_send(self.room, 'engineer', 'Engineer work.', 'grant-fourth',
                                      purpose='correction')
        self.assertEqual(len(self.fake.posts), posts)
        self.assertEqual(self.journal_bytes(), journal)

        self.send('acceptance_review', 'fourth')
        self.finish_review('fourth', 'rejected')
        for intended in ('fourth', 'prior-1', 'grant-fourth'):
            with self.subTest(intended=intended):
                before = self.journal_bytes()
                with self.assertRaises(ao.RoomError):
                    self.audit(intended)
                self.assertEqual(self.journal_bytes(), before)

        audit5 = self.audit('fifth')
        for grant_id in ('grant-fourth', 'fourth', 'prior-1', 'fifth'):
            with self.subTest(grant_id=grant_id):
                before = self.journal_bytes()
                with self.assertRaises(ao.RoomError):
                    self.service.ao_room_acceptance_review_extend(
                        self.room, audit5['audit_sha256'], grant_id,
                        'Actual synthetic user approval for needed additional reviews.',
                        'synthetic-user-message',
                        'A necessary further independent review in the same scope.')
                self.assertEqual(self.journal_bytes(), before)
        before_count = len(self.journal_bytes())
        result = self.service.ao_room_acceptance_review_extend(
            self.room, audit5['audit_sha256'], 'grant-fifth',
            'Actual synthetic user approval for needed additional reviews.',
            'synthetic-user-message',
            'A necessary further independent review in the same scope.')
        self.assertEqual(result['review_request_id'], 'fifth')
        self.assertEqual(len(self.journal_bytes()), before_count + 1)

    def test_p11_deeply_nested_journal_record(self):
        self.grant()
        path = self.journal_dir() / '000001.json'
        original = path.read_bytes()
        try:
            path.write_bytes(('[' * 4000 + ']' * 4000).encode())
            block = self.service.ao_room_status(self.room)['acceptance_review_extension']
            self.assertEqual(block['state'], 'inconsistent')
            posts = len(self.fake.posts)
            with self.assertRaises(ao.RoomError):
                self.service.ao_room_sync(self.room)
            with self.assertRaises(ao.RoomError):
                self.send('acceptance_review', 'fourth')
            self.assertEqual(len(self.fake.posts), posts)
        finally:
            path.write_bytes(original)
        posts = len(self.fake.posts)
        self.send('acceptance_review', 'fourth')
        self.assertEqual(len(self.fake.posts), posts + 1)

    def test_p11_deeply_nested_audit_record(self):
        self.grant()
        audit_path = self.audits_dir() / (json.loads((self.journal_dir() / '000001.json').read_text())
                                          ['inputs']['audit_sha256'] + '.json')
        original = audit_path.read_bytes()
        try:
            audit_path.write_bytes(('[' * 4000 + ']' * 4000).encode())
            block = self.service.ao_room_status(self.room)['acceptance_review_extension']
            self.assertEqual(block['state'], 'inconsistent')
            posts = len(self.fake.posts)
            with self.assertRaises(ao.RoomError):
                self.service.ao_room_sync(self.room)
            with self.assertRaises(ao.RoomError):
                self.send('acceptance_review', 'fourth')
            self.assertEqual(len(self.fake.posts), posts)
        finally:
            audit_path.write_bytes(original)
        posts = len(self.fake.posts)
        self.send('acceptance_review', 'fourth')
        self.assertEqual(len(self.fake.posts), posts + 1)

    def test_p12_historical_grant_evidence_stays_pinned(self):
        self.consumed_fourth()
        first_audit = self.audits_dir() / (json.loads((self.journal_dir() / '000001.json').read_text())
                                           ['inputs']['audit_sha256'] + '.json')
        receipt_path = self.directory() / self.state()['requests']['prior-1']['receipt']
        categories = [
            ('first_grant_audit_modified', first_audit, 'modify'),
            ('first_grant_audit_deleted', first_audit, 'delete'),
            ('first_grant_record', self.journal_dir() / '000001.json', 'modify'),
            ('first_consumption_record', self.journal_dir() / '000002.json', 'modify'),
            ('pre_grant_receipt', receipt_path, 'modify'),
        ]
        for name, path, kind in categories:
            with self.subTest(case=name):
                original = path.read_bytes()
                try:
                    if kind == 'delete':
                        path.unlink()
                    else:
                        path.write_bytes(original + b'\n')
                    block = self.service.ao_room_status(self.room)['acceptance_review_extension']
                    self.assertEqual(block['state'], 'inconsistent')
                    posts = len(self.fake.posts)
                    with self.assertRaises(ao.RoomError):
                        self.service.ao_room_sync(self.room)
                    with self.assertRaises(ao.RoomError):
                        self.service.ao_room_accept(self.room, 'fourth')
                    with self.assertRaises(ao.RoomError):
                        self.audit('fifth')
                    self.assertEqual(len(self.fake.posts), posts)
                finally:
                    path.write_bytes(original)
                self.assertEqual(extension.summary(self.service, self.state())['state'], 'consumed')
        self.assertTrue(self.audit('fifth')['eligible'])

        self.grant('fifth')
        self.send('acceptance_review', 'fifth')
        self.finish_review('fifth', 'rejected')
        for name, path, kind in categories:
            with self.subTest(stage='after_fifth', case=name):
                original = path.read_bytes()
                try:
                    if kind == 'delete':
                        path.unlink()
                    else:
                        path.write_bytes(original + b'\n')
                    self.assertEqual(self.service.ao_room_status(self.room)['acceptance_review_extension']['state'],
                                     'inconsistent')
                    posts = len(self.fake.posts)
                    with self.assertRaises(ao.RoomError):
                        self.service.ao_room_sync(self.room)
                    with self.assertRaises(ao.RoomError):
                        self.service.ao_room_accept(self.room, 'fifth')
                    with self.assertRaises(ao.RoomError):
                        self.audit('sixth')
                    self.assertEqual(len(self.fake.posts), posts)
                finally:
                    path.write_bytes(original)
                self.assertEqual(extension.summary(self.service, self.state())['state'], 'consumed')
        self.assertTrue(self.audit('sixth')['eligible'])

    def test_p12_approved_acceptance_requires_intact_historical_audit(self):
        self.consumed_fourth(decision='approved')
        first_grant = json.loads((self.journal_dir() / '000001.json').read_text())
        audit_path = self.audits_dir() / (first_grant['inputs']['audit_sha256'] + '.json')
        original = audit_path.read_bytes()
        acceptances = copy.deepcopy(self.state()['acceptances'])
        self.assertEqual(acceptances, [])
        posts = len(self.fake.posts)
        for mutation, message in (
                ('modified', 'The acceptance-review audit was modified'),
                ('removed', 'Acceptance-review continuation evidence is unreadable or inconsistent')):
            with self.subTest(mutation=mutation):
                try:
                    if mutation == 'removed':
                        audit_path.unlink()
                    else:
                        value = json.loads(original)
                        value['version'] = 2
                        ao.atomic(audit_path, value)
                    with self.assertRaisesRegex(ao.RoomError, message):
                        self.service.ao_room_accept(self.room, 'fourth')
                    self.assertEqual(self.state()['acceptances'], acceptances)
                    self.assertEqual(len(self.fake.posts), posts)
                finally:
                    audit_path.write_bytes(original)
                accepted = self.service.ao_room_accept(self.room, 'fourth')
                self.assertTrue(accepted['accepted'])
                acceptances = copy.deepcopy(self.state()['acceptances'])
        self.assertEqual(len(acceptances), 1)

    def test_p13_detached_modified_and_edited_linkage(self):
        self.consumed_fourth()
        original_state = self.state_bytes()
        base_state = self.state()
        fourth = base_state['requests']['fourth']
        prior = base_state['requests']['prior-1']
        mutations = [
            ('detached',
             lambda state: [state['requests']['fourth'].pop(key, None)
                            for key in ('acceptance_review_grant', 'acceptance_review_consumption',
                                        'acceptance_review_consumption_sha256')]),
            ('consumption_digest',
             lambda state: state['requests']['fourth'].__setitem__('acceptance_review_consumption_sha256', '0' * 64)),
            ('moved_linkage',
             lambda state: [state['requests']['prior-1'].__setitem__(key, copy.deepcopy(fourth[key]))
                            for key in ('acceptance_review_grant', 'acceptance_review_consumption',
                                        'acceptance_review_consumption_sha256')]),
            ('role_changed',
             lambda state: state['requests']['fourth'].__setitem__('role', 'engineer')),
            ('creation_order',
             lambda state: state['requests']['fourth'].__setitem__('created_order', 1)),
            ('removed',
             lambda state: state['requests'].pop('fourth')),
        ]
        for name, change in mutations:
            with self.subTest(case=name):
                try:
                    state = self.state()
                    change(state)
                    self.write_state(state)
                    self.assert_inconsistent_refusals('fifth', 'fourth')
                finally:
                    self.write_state_bytes(original_state)

    def test_p14a_grant_record_write_failure(self):
        inputs = self.grant_inputs()
        original_store = extension._store_once
        posts = len(self.fake.posts)

        def fail_first(path, value):
            if path.name == '000001.json':
                raise OSError('synthetic journal write failure')
            return original_store(path, value)

        with patch.object(extension, '_store_once', side_effect=fail_first):
            with self.assertRaisesRegex(ao.RoomError, 'could not be stored durably'):
                self.service.ao_room_acceptance_review_extend(**inputs)
        self.assertFalse(self.journal_dir().exists() and os.listdir(self.journal_dir()))
        self.assertIsNone(self.state().get(extension.KEY))
        self.assertEqual(len(self.fake.posts), posts)
        result = self.service.ao_room_acceptance_review_extend(**inputs)
        self.assertEqual(result['remaining_additional_reviews'], 1)
        self.assertEqual(len(os.listdir(self.journal_dir())), 1)
        self.assertEqual(len(self.state()[extension.KEY]['entries']), 1)

    def test_p14b_consumption_record_write_failure(self):
        self.grant()
        original_store = extension._store_once
        posts = len(self.fake.posts)
        journal = self.journal_bytes()

        def fail_consumption(path, value):
            if path.name == '000002.json':
                raise OSError('synthetic consumption write failure')
            return original_store(path, value)

        with patch.object(extension, '_store_once', side_effect=fail_consumption):
            with self.assertRaisesRegex(ao.RoomError, 'could not be stored durably'):
                self.send('acceptance_review', 'fourth')
        self.assertEqual(len(self.fake.posts), posts)
        self.assertNotIn('fourth', self.state()['requests'])
        self.assertEqual(self.journal_bytes(), journal)
        self.assertEqual(extension.summary(self.service, self.state())['state'], 'unconsumed')
        self.send('acceptance_review', 'fourth')
        self.assertEqual(len(self.fake.posts), posts + 1)
        self.assertEqual(extension.summary(self.service, self.state())['state'], 'consumed')

    def test_p15_restart_pending_grant(self):
        inputs = self.grant_inputs()
        saved = self.service.save
        def failed_projection(directory, state):
            if state.get(extension.KEY):
                raise OSError('synthetic interruption before projection')
            return saved(directory, state)
        with patch.object(self.service, 'save', side_effect=failed_projection):
            with self.assertRaises(OSError):
                self.service.ao_room_acceptance_review_extend(**inputs)
        receipt = (self.journal_dir() / '000001.json').read_bytes()
        restarted = ao.Service(self.home, lambda url: self.fake)
        status = restarted.ao_room_status(self.room)['acceptance_review_extension']
        self.assertEqual(status['state'], 'pending_uncommitted')
        with self.assertRaisesRegex(ao.RoomError, 'belongs to another payload'):
            restarted.ao_room_acceptance_review_extend(**{**inputs, 'authorization': 'Changed.'})
        posts = len(self.fake.posts)
        journal = self.journal_bytes()
        with self.assertRaisesRegex(ao.RoomError, 'uncommitted acceptance-review grant receipt exists'):
            restarted.ao_room_send(self.room, 'reviewer', self.message, 'fourth',
                                   purpose='acceptance_review')
        self.assertEqual(len(self.fake.posts), posts)
        self.assertEqual(self.journal_bytes(), journal)
        self.assertNotIn('fourth', self.state()['requests'])
        reconciled = restarted.ao_room_acceptance_review_extend(**inputs)
        self.assertEqual(reconciled['remaining_additional_reviews'], 1)
        self.assertEqual((self.journal_dir() / '000001.json').read_bytes(), receipt)

    def test_p15_restart_consumption_without_projection(self):
        self.grant()
        posts = len(self.fake.posts)
        saved = self.service.save
        def fail_after_consumption(directory, state):
            if 'fourth' in state['requests']:
                raise OSError('synthetic interruption before request projection')
            return saved(directory, state)
        with patch.object(self.service, 'save', side_effect=fail_after_consumption):
            with self.assertRaises(OSError):
                self.send('acceptance_review', 'fourth')
        restarted = ao.Service(self.home, lambda url: self.fake)
        status = restarted.ao_room_status(self.room)['acceptance_review_extension']
        self.assertEqual(status['state'], 'inconsistent')
        self.assertNotIn('fourth', self.state()['requests'])
        fragment = 'is durable but its request projection is missing'
        self.assertIn(fragment, status['error'])
        journal = self.journal_bytes()
        for operation in (
                lambda: restarted.ao_room_send(self.room, 'reviewer', self.message, 'fourth',
                                               purpose='acceptance_review'),
                lambda: restarted.ao_room_send(self.room, 'reviewer', self.message, 'fourth',
                                               purpose='acceptance_review'),
                lambda: restarted.ao_room_sync(self.room),
                lambda: restarted.ao_room_acceptance_review_audit(
                    self.room, 'fifth', ao.digest(self.message.encode()), str(self.database),
                    'synthetic-native-engineer', 'synthetic-native-reviewer')):
            with self.assertRaisesRegex(ao.RoomError, fragment):
                operation()
            self.assertEqual(len(self.fake.posts), posts)
            self.assertEqual(self.journal_bytes(), journal)

    def test_p15_restart_known_send_no_duplicate(self):
        self.grant()
        self.fake.lose_ack = True
        first = self.send('acceptance_review', 'fourth')
        restarted = ao.Service(self.home, lambda url: self.fake)
        posts = len(self.fake.posts)
        self.assertEqual(restarted.ao_room_send(self.room, 'reviewer', self.message, 'fourth',
                                                purpose='acceptance_review'), first)
        self.assertEqual(len(self.fake.posts), posts)
        journal = self.journal_bytes()
        observed_turn = self.fake.snapshots['reviewer']['turns'][-1]['id']
        restarted.ao_room_sync(self.room)
        self.assertEqual(self.state()['requests']['fourth']['turn_id'], observed_turn)
        self.assertEqual(extension.summary(restarted, self.state())['state'], 'consumed')
        self.assertEqual(len(self.fake.posts), posts)
        self.assertEqual(self.journal_bytes(), journal)


class P10CharterLaneTests(MatrixFixture):
    spec_review_count = 3
    last_review_decision = 'approved'

    def test_p10_charter_lane_mutual_exclusion(self):
        accepted = self.service.ao_room_accept(self.room, 'prior-3')
        self.assertTrue(accepted['accepted'])
        self.grant()
        before = self.state_bytes()
        lane = self.lane_snapshot()
        posts = len(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, 'unused acceptance-review grant freezes'):
            self.spec(2)
        self.assertEqual(self.state_bytes(), before)
        with self.assertRaisesRegex(ao.RoomError, 'Register the exact next charter revision'):
            self.service.ao_room_spec_review_extension_audit(
                self.room, 2, 'a' * 64, accepted['candidate_sha256'],
                'synthetic-native-engineer', str(self.database))
        self.assertFalse((self.directory() / 'spec-review-extension').exists())
        self.assertEqual(len(self.fake.posts), posts)
        self.assertEqual(self.state_bytes(), before)
        self.assertEqual(self.lane_snapshot(), lane)

        self.assertEqual(extension.summary(self.service, self.state())['state'], 'unconsumed')


class P10NoGrantTests(AcceptanceContinuationFixture):
    spec_review_count = 3
    last_review_decision = 'approved'

    def test_p10_registered_charter_blocks_acceptance_audit(self):
        self.spec(2)
        with self.assertRaisesRegex(ao.RoomError, 'Create a passed verification checkpoint first'):
            self.audit()
        self.assertFalse((self.directory() / extension.BASE).exists())


class P16DefaultPurposeTests(AcceptanceContinuationFixture):
    prior_review_default_purpose = True

    def test_p16_default_purpose_accounting(self):
        self.assertEqual(self.service.ao_room_status(self.room)['acceptance_review_attempts'], 3)
        posts = len(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, 'Three review attempts exhausted'):
            self.service.ao_room_send(self.room, 'reviewer', self.message, 'fourth')
        self.assertEqual(len(self.fake.posts), posts)
        self.assertTrue(self.audit()['eligible'])
        self.grant()
        self.send('acceptance_review', 'fourth')
        self.assertEqual(self.state()['requests']['fourth']['purpose'], 'acceptance_review')


class P16NoImplicitJournalTests(AcceptanceContinuationFixture):
    def test_p16_no_implicit_journal(self):
        posts = len(self.fake.posts)
        self.service.ao_room_status(self.room)
        self.assertFalse((self.directory() / extension.BASE).exists())
        self.service.ao_room_sync(self.room)
        self.assertFalse((self.directory() / extension.BASE).exists())
        with self.assertRaisesRegex(ao.RoomError, 'Three review attempts exhausted'):
            self.service.ao_room_send(self.room, 'reviewer', self.message, 'fourth')
        self.assertEqual(len(self.fake.posts), posts)
        self.assertFalse((self.directory() / extension.BASE).exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
