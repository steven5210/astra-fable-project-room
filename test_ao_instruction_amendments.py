import json

import ao_project_room as ao
from test_ao_normal import Fixture


class InstructionAmendmentTests(Fixture):
    def setUp(self):
        super().setUp(); self.room = self.open(); self.spec(); self.bind(); self.agree(); self.implement()

    def test_staging_does_not_dispatch_and_only_new_instruction_is_delivered_once(self):
        before = self.state(); count = len(self.fake.posts)
        args = (self.room, 'routing-clarification', 'Personal review does not assign document assembly to Fable.', 'User asked to fix the authorship override')
        first = self.service.ao_room_instruction_stage(*args)
        self.assertEqual(self.service.ao_room_instruction_stage(*args), first)
        self.assertEqual(len(self.fake.posts), count)
        self.assertEqual(self.state()['requests'], before['requests'])
        self.service.ao_room_send(self.room, 'engineer', 'Continue.', 'resume', purpose='correction')
        self.assertEqual(self.state()['requests']['resume']['text'], args[2] + '\nContinue.')
        self.fake.finish('engineer', json.dumps(self.report())); self.service.ao_room_sync(self.room)
        self.service = ao.Service(self.home, lambda url: self.fake)
        self.service.ao_room_send(self.room, 'engineer', 'Continue.', 'next', purpose='correction')
        self.assertEqual(self.state()['requests']['next']['text'], 'Continue.')

    def test_stage_never_releases_quota_or_rewrites_same_identity(self):
        self.send('correction'); self.fake.finish('engineer', '')
        self.fake.snapshots['engineer']['turns'][-1]['error'] = {'type': 'rate_limit'}
        self.service.ao_room_sync(self.room)
        self.service.ao_room_instruction_stage(self.room, 'new-rules', 'Only a new rule.', 'User approved')
        with self.assertRaises(ao.RoomError):
            self.service.ao_room_instruction_stage(self.room, 'new-rules', 'Different rule.', 'User approved')
        with self.assertRaisesRegex(ao.RoomError, 'semantic hold'):
            self.service.ao_room_send(self.room, 'engineer', 'Continue.', 'next', purpose='correction')

    def test_tampered_amendment_refuses_before_send(self):
        record = self.service.ao_room_instruction_stage(self.room, 'new-rules', 'New rule.', 'User approved')
        (self.directory() / record['path']).write_text('{}')
        count = len(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, 'evidence changed'): self.send('correction')
        self.assertEqual(len(self.fake.posts), count)
