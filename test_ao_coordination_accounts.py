"""Single/dual coordination account mode: opt-in coordination_accounts in ao/config.json.

Single (default) keeps coordination inside one account: an owner and an assignment room on
different profiles refuse admission. Dual requires both rooms pinned to the two configured
profiles with distinct live identities. One engaged assignment per owner in either mode,
atomic under the admission lock; draining work never re-reads the mode.
"""
import hashlib
import json
import os
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

import ao_project_room as ao
import ao_account_profiles as ap
import ao_coordination
import ao_native_outcome
import ao_workflow
import project_room
from room import RoomError
from test_ao_coordination import CoordinationFixture

B_UUID = "b-account-uuid"
B_DIGEST = hashlib.sha256(B_UUID.encode()).hexdigest()


class DualAccountCoordinationFixture(CoordinationFixture):
    """A coordination fixture on a synthetic HOME with the default login plus profile 'b'."""

    def _populate_root(self):
        super()._populate_root()
        self.user_home = self.root / "user-home"
        self.user_home.mkdir(mode=0o700)
        (self.user_home / ".claude").mkdir(mode=0o700)
        (self.user_home / ".claude.json").write_text(json.dumps(
            {"oauthAccount": {"accountUuid": "default-account-uuid"}}))
        self.claude_env = self.user_home / ".claude"
        config_path = self.home / "config.json"
        value = ao.read(config_path)
        value["claude_config_dir"] = str(self.claude_env)
        ao.atomic(config_path, value)
        self.profile_b = self.root / "profile-b"
        self.profile_b.mkdir(mode=0o700)
        (self.profile_b / ".claude.json").write_text(json.dumps(
            {"oauthAccount": {"accountUuid": B_UUID, "emailAddress": "profile-owner@example.invalid"}}))
        with patch.dict(os.environ, {"HOME": str(self.user_home)}):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            result = project_room.Service(str(self.home)).setup(
                claude_bin=str(self.fixture_claude), delegate_provider="none",
                claude_profile="b", profile_dir=str(self.profile_b))
        self.assertEqual(result["enrolled_profile"], "b")

    def _activate(self):
        super()._activate()
        for key in (*ap.CREDENTIAL_ENV, ap.CUSTOM_HEADERS, "CLAUDE_CODE_MANAGED_SETTINGS_PATH"):
            os.environ.pop(key, None)
        patched = patch.dict(os.environ, {"HOME": str(self.user_home)})
        patched.start()
        self.addCleanup(patched.stop)

    def env(self, mapping=None):
        if mapping is None:
            self.fake.config.pop("env", None)
        else:
            self.fake.config["env"] = mapping

    def _b_env(self):
        self.env({"CLAUDE_CONFIG_DIR": str(self.profile_b)})

    def _owner_env(self):
        try:
            pin = self.state().get("account_profile")
        except Exception:
            pin = None
        name = pin.get("name") if isinstance(pin, dict) else None
        self._b_env() if name == "b" else self.env(None)

    # Pin checks run at prepare, bind and every send: a default/unpinned owner needs no
    # CLAUDE_CONFIG_DIR, the b-pinned assignment needs exactly its pinned directory.
    def send(self, purpose, key=None):
        self._owner_env()
        return super().send(purpose, key)

    def bind(self):
        self._owner_env()
        return super().bind()

    def send_b(self, purpose, key=None):
        self._b_env()
        return super().send_b(purpose, key)

    def bind_b(self):
        self._b_env()
        return super().bind_b()

    def write_policy(self, value):
        path = self.service.root / "config.json"
        config = ao.read(path) if path.exists() else {}
        config["coordination_accounts"] = value
        ao.atomic(path, config)

    def _second_owner_pinned(self, feature, profile):
        """_second_owner for a pinned room: its own linked worktree, agents and native source."""
        import subprocess
        import datetime as _dt
        wt2 = self.root / ('wt-' + feature)
        subprocess.run(['git', '-C', str(self.repo), 'worktree', 'add', str(wt2), 'HEAD'],
                       check=True, capture_output=True)
        room = self._open(feature, profile)
        self.service.ao_room_spec_put(room, 1, 'Second owner spec.', self.gates,
                                      'Astra approves this scope')
        env = {"CLAUDE_CONFIG_DIR": str(self.profile_b)} if profile == "b" else None
        self.env(env)
        self.service.ao_room_prepare(room, str(wt2))
        self.fake.add('engineer-' + feature, 'claude-code')
        self.fake.add('reviewer-' + feature)
        self.fake.workspaces['engineer-' + feature] = wt2
        self.fake.workspaces['reviewer-' + feature] = wt2
        self.service.ao_room_bind(room, 'engineer', 'engineer-' + feature,
                                  ao_workflow.FABLE_MODEL, 'max')
        self.service.ao_room_bind(room, 'reviewer', 'reviewer-' + feature, 'astra', 'max')
        directory = self.directory(room)
        state = ao.read(directory / 'state.json')
        binding = state['bindings']['engineer']
        project_dir = self.native_config_root() / 'projects' / str(wt2).replace('/', '-')
        project_dir.mkdir(parents=True, exist_ok=True)
        transcript = project_dir / (feature + '.jsonl')
        transcript.write_text('')
        self.owners[binding['session_id']] = {
            'id': binding['session_id'], 'project_id': state['ao_project_id'],
            'harness': 'claude-code', 'session_mode': 'chat', 'is_terminated': 0,
            'activity_state': 'idle', 'workspace_path': str(wt2),
            'provider_conversation_id': feature, 'controller_generation': 'generation-1',
            'ao_conversation_id': binding['conversation_id'], 'active_branch_id': binding['branch_id'],
            'branch_provider_conversation_id': feature, 'branch_session_id': binding['session_id'],
            'strategy': 'native', 'replay_truncated': 0}
        ao_native_outcome.preflight_source(directory, state, str(self.root / 'owner.db'),
                                           str(transcript))
        ao.atomic(directory / 'state.json', state)
        self.service.ao_room_send(room, 'engineer', 'Review the registered specification.',
                                  'spec_review', purpose='spec_review')
        request = ao.read(directory / 'state.json')['requests']['spec_review']
        model = (request.get('engineering_resolution') or {}).get('expected_model') or request['model']
        from datetime import datetime, timezone
        stamp = lambda s: min(datetime.fromtimestamp(request['created_at'] + s, timezone.utc),
                              datetime.now(timezone.utc)).isoformat()
        transcript.write_text(''.join(json.dumps(e) + '\n' for e in [
            {'type': 'user', 'uuid': 'spec_review-caller', 'sessionId': feature,
             'timestamp': stamp(0.001), 'cwd': str(wt2), 'isSidechain': False,
             'origin': {'kind': 'human'}, 'message': {'role': 'user', 'content': request['text']}},
            {'type': 'assistant', 'uuid': 'spec_review-reply', 'sessionId': feature,
             'timestamp': stamp(0.002), 'cwd': str(wt2),
             'message': {'role': 'assistant', 'model': model, 'id': 'spec_review-m0',
                         'content': [{'type': 'text', 'text': 'Done'}], 'stop_reason': 'end_turn'}}]))
        spec = self.service.spec(directory, ao.read(directory / 'state.json'))
        self.fake.finish('engineer-' + feature, json.dumps(
            {'interpretation': 'Implement the supplied pure behavior.', 'findings': [],
             'decision': 'accept', 'spec_revision': 1, 'spec_sha256': spec['sha256']}))
        self.service.ao_room_sync(room)
        self.service.ao_room_handoff(room, str(wt2))
        return room

    def _open(self, feature, profile=None):
        args = {"account_profile": profile} if profile else {}
        return self.service.ao_room_open(
            str(self.repo), feature, "project", "User authorized this feature",
            "http://127.0.0.1:1234", delegate_provider='none', **args)['room_id']

    def open_rooms_pinned(self, owned=('owned',), owner_profile='default',
                          assignment_profile='b', admission=True):
        self.qualify()
        self.room = self._open('owner', owner_profile)
        self.spec()
        self.bind()
        self.agree()
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.assign_room = self._open('assignment', assignment_profile)
        self.spec_b()
        self.bind_b()
        if not admission:
            return None
        return self.service.ao_room_assignment_open(
            self.room, 'A1', self.assign_room, list(owned),
            'The interface contract for the owned paths.',
            'User authorized this bounded assignment')


class AccountModeTests(DualAccountCoordinationFixture):
    """The opt-in coordination_accounts mode in PROJECT_ROOM_HOME/ao/config.json."""

    DUAL = {"mode": "dual", "owner_profile": "default", "assignment_profile": "b"}

    def test_absent_config_reports_single_and_unpinned_admission_works(self):
        # (a) no key: effective single, null names, the ordinary unpinned path admits.
        self.open_rooms()
        status = self.service.ao_room_status(self.room)
        self.assertEqual(status["coordination"]["account_policy"],
                         {"mode": "single", "owner_profile": None, "assignment_profile": None})
        self.assertEqual(self.lifecycle('A1')['state'], 'active')
        self.assertEqual(ao.read(self.assignments_root() / 'A1' / 'lineage.json')
                         ["account_policy"]["mode"], "single")

    def test_invalid_configs_refuse_admission_but_status_still_reads(self):
        # (b) mode/shape defects refuse admission; the neutral read never raises.
        self.open_rooms_pinned(admission=False)
        for bad in ({"mode": "triple", "owner_profile": "default", "assignment_profile": "b"},
                    {"mode": "dual", "owner_profile": "default", "assignment_profile": None},
                    {"mode": "dual", "owner_profile": "b", "assignment_profile": "b"}):
            with self.subTest(bad=bad):
                self.write_policy(bad)
                with self.assertRaisesRegex(RoomError, 'coordination_accounts.*invalid'):
                    self.service.ao_room_assignment_open(
                        self.room, 'A1', self.assign_room, ['owned'], 'i', 'a')
                status = self.service.ao_room_status(self.room)["coordination"]["account_policy"]
                self.assertEqual(status["mode"], "invalid")
                self.assertTrue(status["error"])

    def test_dual_same_identity_default_and_b_refuses(self):
        # (c) the saved default login already enrolled as b: #220's own message surfaces.
        self.open_rooms_pinned(admission=False)
        self.write_policy(self.DUAL)
        (self.user_home / ".claude.json").write_text(json.dumps(
            {"oauthAccount": {"accountUuid": B_UUID}}))
        with self.assertRaisesRegex(RoomError, 'also enrolled as profile b'):
            self.service.ao_room_assignment_open(
                self.room, 'A1', self.assign_room, ['owned'], 'i', 'a')

    def test_dual_requires_pins_and_configured_names(self):
        # (d) unpinned owner refuses; a pin name other than the configured one mismatches.
        self.open_rooms_pinned(owner_profile=None, admission=False)
        self.write_policy(self.DUAL)
        with self.assertRaisesRegex(RoomError, 'room_not_pinned'):
            self.service.ao_room_assignment_open(
                self.room, 'A1', self.assign_room, ['owned'], 'i', 'a')
        # Owner pinned 'b' while the config names 'default'.
        b_owner = self._second_owner_pinned('owner-b', 'b')
        with self.assertRaisesRegex(RoomError, 'profile_mismatch'):
            self.service.ao_room_assignment_open(
                b_owner, 'A1', self.assign_room, ['owned'], 'i', 'a')

    def test_identity_drift_refuses_without_touching_the_pins(self):
        # (e) a live identity change since pinning refuses; saved pins stay byte-identical.
        self.write_policy(self.DUAL)
        self.open_rooms_pinned()
        owner_pin = self.state()["account_profile"]
        assign_pin = self.state_b()["account_profile"]
        drift_room = self._open('assignment-drift', 'b')
        self.service.ao_room_spec_put(drift_room, 1, 'Drift probe.', self.gates_b,
                                      'Astra approves this scope')
        (self.profile_b / ".claude.json").write_text(json.dumps(
            {"oauthAccount": {"accountUuid": "drifted-uuid"}}))
        with self.assertRaisesRegex(RoomError, 'account_identity_changed'):
            self.service.ao_room_assignment_open(
                self.room, 'A2', drift_room, ['unrelated'], 'i', 'a')
        self.assertEqual(self.state()["account_profile"], owner_pin)
        self.assertEqual(self.state_b()["account_profile"], assign_pin)

    def test_one_engaged_assignment_per_owner_sequential_and_concurrent(self):
        # (f) the cap is atomic: sequential refusal, and a two-thread race admits one.
        self.write_policy(self.DUAL)
        self.open_rooms_pinned()
        wt2 = self.root / 'assign2'
        import subprocess
        subprocess.run(['git', '-C', str(self.repo), 'worktree', 'add', str(wt2), 'HEAD'],
                       check=True, capture_output=True)
        self.fake.add('engineer-c', 'claude-code')
        self.fake.add('reviewer-c')
        self.fake.workspaces['engineer-c'] = wt2
        self.fake.workspaces['reviewer-c'] = wt2
        assign2 = self._open('assignment-two', 'b')
        self.service.ao_room_spec_put(assign2, 1, 'Second assignment.', self.gates_b,
                                      'Astra approves this scope')
        self._b_env()
        self.service.ao_room_prepare(assign2, str(wt2))
        self.service.ao_room_bind(assign2, 'engineer', 'engineer-c', ao_workflow.FABLE_MODEL, 'max')
        self.service.ao_room_bind(assign2, 'reviewer', 'reviewer-c', 'astra', 'max')
        with self.assertRaisesRegex(RoomError, 'owner_assignment_cap'):
            self.service.ao_room_assignment_open(
                self.room, 'A2', assign2, ['unrelated'], 'interface', 'authorization')
        # Concurrent: a fresh unpinned owner under single mode races disjoint-path admissions
        # on two free assignment rooms; exactly one wins, the loser names the cap.
        self.write_policy({"mode": "single", "owner_profile": None, "assignment_profile": None})
        race_owner = self._second_owner('owner-race')
        assign3 = self._open('assignment-three')
        self.service.ao_room_spec_put(assign3, 1, 'Third assignment.', self.gates_b,
                                      'Astra approves this scope')
        other_service = ao.Service(self.home, lambda url: self.fake)
        outcomes = {}

        def admit(tag, service, assignment_id, room):
            try:
                outcomes[tag] = service.ao_room_assignment_open(
                    race_owner, assignment_id, room, ['cap-' + tag], 'interface', 'authorization')
            except RoomError as exc:
                outcomes[tag] = exc

        assign4 = self._open('assignment-four')
        self.service.ao_room_spec_put(assign4, 1, 'Fourth assignment.', self.gates_b,
                                      'Astra approves this scope')
        threads = [threading.Thread(target=admit, args=('a', self.service, 'AC', assign3)),
                   threading.Thread(target=admit, args=('b', other_service, 'AD', assign4))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        wins = [v for v in outcomes.values() if isinstance(v, dict)]
        losses = [v for v in outcomes.values() if isinstance(v, RoomError)]
        self.assertEqual(len(wins), 1)
        self.assertEqual(len(losses), 1)
        self.assertIn('owner_assignment_cap', str(losses[0]))
        self.assertEqual(len([p for p in self.assignments_root().glob('A[CD]/lineage.json')]), 1)

    def test_disabling_dual_keeps_outstanding_work_and_blocks_new(self):
        # (g) admitted work drains under its recorded policy while single blocks new duals.
        self.write_policy(self.DUAL)
        self.open_rooms_pinned()
        self.agree_b()
        self.service.ao_room_handoff(self.assign_room, str(self.assign_wt))
        self.implement_b()
        self.review_b()
        self.accept_b()
        self.write_policy({"mode": "single", "owner_profile": None, "assignment_profile": None})
        assign2 = self._open('assignment-two', 'b')
        self.service.ao_room_spec_put(assign2, 1, 'Second assignment.', self.gates_b,
                                      'Astra approves this scope')
        with self.assertRaisesRegex(RoomError, 'dual_account_requires_dual_mode'):
            self.service.ao_room_assignment_open(
                self.room, 'A2', assign2, ['unrelated'], 'i', 'a')
        record = self.service.ao_room_handback_record(self.assign_room, 'A1', 'acceptance_review')
        preflight = self.service.ao_room_import_preflight(self.room, 'A1', record['handback_sha256'])
        self.git(self.repo, 'merge', '--no-ff', '--no-edit', record['commit'])
        imported = self.service.ao_room_import_record(self.room, 'A1', preflight['preflight_sha256'],
                                                      'git merge ' + record['commit'])
        self.assertEqual(imported['kind'], 'faithful')
        lineage = ao.read(self.assignments_root() / 'A1' / 'lineage.json')
        self.assertEqual(lineage["account_policy"]["mode"], "dual")
        status = self.service.ao_room_status(self.assign_room)["coordination"]
        self.assertEqual(status["account_policy"]["mode"], "single")
        entries = {e["assignment_id"]: e for e in status["assignments"]}
        self.assertEqual(entries["A1"]["account_policy"]["mode"], "dual")

    def test_single_mode_profile_rules(self):
        # (h) owner_profile applies only in single; cross-profile pairs need dual.
        self.open_rooms_pinned(owner_profile='b', assignment_profile='b', admission=False)
        self.write_policy({"mode": "single", "owner_profile": "default",
                           "assignment_profile": None})
        with self.assertRaisesRegex(RoomError, 'profile_mismatch'):
            self.service.ao_room_assignment_open(
                self.room, 'A1', self.assign_room, ['owned'], 'i', 'a')
        self.write_policy({"mode": "single", "owner_profile": None, "assignment_profile": None})
        default_owner = self._second_owner('owner-h2')
        with self.assertRaisesRegex(RoomError, 'dual_account_requires_dual_mode'):
            self.service.ao_room_assignment_open(
                default_owner, 'A1', self.assign_room, ['owned'], 'i', 'a')


if __name__ == '__main__':
    unittest.main()
