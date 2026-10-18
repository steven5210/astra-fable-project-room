"""AO room integration contracts for fixed Claude account profiles (fake AO, synthetic homes only)."""
import contextlib
import hashlib
import io
import json
import os
import unittest
from unittest.mock import patch

import ao_account_profiles as ap
import ao_delegates
import ao_project_room as ao
import ao_usage_audit
import ao_workflow
import project_room
from room import RoomError
from test_ao_normal import Fixture


B_UUID = "b-account-uuid"
B_DIGEST = hashlib.sha256(B_UUID.encode()).hexdigest()


class AccountFixture(Fixture):
    """The shared fixture plus a synthetic HOME holding the default login and one enrolled profile."""

    def _populate_root(self):
        super()._populate_root()
        self.user_home = self.root / "user-home"
        self.user_home.mkdir(mode=0o700)
        (self.user_home / ".claude").mkdir(mode=0o700)
        (self.user_home / ".claude.json").write_text(json.dumps(
            {"oauthAccount": {"accountUuid": "default-account-uuid"}}))
        # The recorded default directory becomes the synthetic HOME login so `default` pins resolve.
        self.claude_env = self.user_home / ".claude"
        config_path = self.home / "config.json"
        value = ao.read(config_path)
        value["claude_config_dir"] = str(self.claude_env)
        ao.atomic(config_path, value)
        self.profile_b = self.root / "profile-b"
        self.profile_b.mkdir(mode=0o700)
        (self.profile_b / ".claude.json").write_text(json.dumps(
            {"oauthAccount": {"accountUuid": B_UUID, "emailAddress": "profile-owner@example.invalid"}}))
        with patch.dict(os.environ):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            result = project_room.Service(str(self.home)).setup(
                claude_bin=str(self.fixture_claude), delegate_provider="none",
                claude_profile="b", config_dir=str(self.profile_b))
        self.assertEqual(result["enrolled_profile"], "b")
        self.assertEqual(result["claude_profiles"], ["b"])

    def _activate(self):
        super()._activate()
        for key in (*ap.CREDENTIAL_ENV, ap.CUSTOM_HEADERS, "CLAUDE_CODE_MANAGED_SETTINGS_PATH"):
            os.environ.pop(key, None)
        patched = patch.dict(os.environ, {"HOME": str(self.user_home)})
        patched.start()
        self.addCleanup(patched.stop)

    def open_pinned(self, name="b", feature="pinned"):
        self.qualify()
        return self.service.ao_room_open(str(self.repo), feature, "project", "User authorized this feature",
                                         "http://127.0.0.1:1234", delegate_provider="none",
                                         account_profile=name)["room_id"]

    def prepare(self):
        return self.service.ao_room_prepare(self.room, str(self.repo))

    def env(self, mapping=None):
        if mapping is None:
            self.fake.config.pop("env", None)
        else:
            self.fake.config["env"] = mapping


class RoomOpenTests(AccountFixture):
    def test_pinned_room_records_profile(self):
        self.room = self.open_pinned()
        pin = self.state()["account_profile"]
        self.assertEqual(pin["name"], "b")
        self.assertEqual(pin["config_dir_exact"], str(self.profile_b))
        self.assertEqual(pin["claude_config_dir"], str(self.profile_b))
        self.assertEqual(pin["global_config_path"], str(self.profile_b / ".claude.json"))
        self.assertEqual(pin["identity_digest"], B_DIGEST)
        self.assertEqual(pin["keychain_service"], ap.keychain_service(str(self.profile_b)))
        settings = ao.read(self.directory() / "settings.json")
        self.assertEqual(settings["delegate_provider"], "none")
        self.assertEqual(settings["claude_config_dir"], str(self.profile_b))
        self.assertEqual(settings["claude_config_dir_override"], str(self.profile_b))
        self.assertEqual(settings["account_profile"]["name"], "b")
        status = self.service.ao_room_status(self.room)
        self.assertEqual(status["account_profile"]["name"], "b")
        listed = {item["room_id"]: item for item in self.service.ao_room_list()["rooms"]}
        self.assertEqual(listed[self.room]["account_profile"], "b")

    def test_unpinned_room_is_byte_identical(self):
        self.room = self.open()
        path = self.directory() / "settings.json"
        self.assertEqual(path.read_bytes(), b'{"delegate_provider": "none"}\n')
        self.assertNotIn("account_profile", self.state())
        status = self.service.ao_room_status(self.room)
        self.assertEqual(status["account_profile"], "unrecorded")
        listed = {item["room_id"]: item for item in self.service.ao_room_list()["rooms"]}
        self.assertEqual(listed[self.room]["account_profile"], "unrecorded")

    def test_existing_room_pin_is_immutable(self):
        self.room = self.open_pinned()
        for other in ("default", "unknown-name"):
            with self.subTest(other=other):
                with self.assertRaises(RoomError):
                    self.service.ao_room_open(str(self.repo), "pinned", "project",
                                              "User authorized this feature", "http://127.0.0.1:1234",
                                              delegate_provider="none", account_profile=other)
        # Reopening with the identical pin stays idempotent.
        same = self.service.ao_room_open(str(self.repo), "pinned", "project",
                                         "User authorized this feature", "http://127.0.0.1:1234",
                                         delegate_provider="none", account_profile="b")
        self.assertEqual(same["room_id"], self.room)

    def test_astra_led_refuses_account_profile(self):
        with self.assertRaises(RoomError):
            self.service.ao_room_open(str(self.repo), "astra", "project",
                                      "User authorized this feature", "http://127.0.0.1:1234",
                                      workflow="astra_led",
                                      exception_authorization="User decided the task exception",
                                      account_profile="b")

    def test_unknown_profile_refused(self):
        self.qualify()
        with self.assertRaises(RoomError):
            self.service.ao_room_open(str(self.repo), "unknown", "project",
                                      "User authorized this feature", "http://127.0.0.1:1234",
                                      delegate_provider="none", account_profile="zzz")
        self.assertEqual(self.service.ao_room_list()["count"], 0)

    def test_default_pin(self):
        self.room = self.open_pinned("default")
        pin = self.state()["account_profile"]
        self.assertEqual(pin["name"], "default")
        self.assertIsNone(pin["config_dir_exact"])
        self.assertEqual(pin["claude_config_dir"], str((self.user_home / ".claude").resolve()))
        settings = ao.read(self.directory() / "settings.json")
        self.assertIsNone(settings["claude_config_dir_override"])
        self.spec()
        self.env()  # the default pin refuses any explicit key, so the project env carries none
        self.prepare()
        evidence = ao.read(self.directory() / "preparation.json")["account_profile"]
        self.assertEqual(evidence["name"], "default")
        self.assertIsNone(evidence["config_dir_exact"])
        self.assertEqual(evidence["trust"]["explicit_setup"]["env"], {})


class BoundaryTests(AccountFixture):
    def setUp(self):
        super().setUp()
        self.room = self.open_pinned()
        self.spec()

    def exact(self):
        return {"CLAUDE_CONFIG_DIR": str(self.profile_b)}

    def test_prepare_records_profile_evidence(self):
        self.env(self.exact())
        self.prepare()
        evidence = ao.read(self.directory() / "preparation.json")["account_profile"]
        self.assertEqual(evidence["name"], "b")
        self.assertEqual(evidence["identity_digest"], B_DIGEST)
        self.assertEqual(evidence["config_dir_exact"], str(self.profile_b))
        self.assertIs(evidence["project_env_checked"], True)
        self.assertIs(evidence["process_env_checked"], True)
        self.assertEqual(evidence["credential_sources_checked"],
                         ["AO project environment", "process environment", "user", "managed", "project",
                          "local", ".credentials.json"])
        trust = evidence["trust"]
        self.assertIs(trust["trust_recorded"], False)
        self.assertEqual(trust["explicit_setup"]["env"], {"CLAUDE_CONFIG_DIR": str(self.profile_b)})
        self.assertEqual(trust["explicit_setup"]["cwd"], str(self.repo))

    def test_prepare_refuses_env_mismatch(self):
        for env in (None, {}, {"CLAUDE_CONFIG_DIR": "/elsewhere"},
                    {"CLAUDE_CONFIG_DIR": str(self.profile_b) + "/"}, {"CLAUDE_CONFIG_DIR": 7}):
            with self.subTest(env=env):
                self.env(env)
                self.assertRaises(RoomError, self.prepare)

    def test_prepare_refuses_credential_sources(self):
        self.env({**self.exact(), "ANTHROPIC_API_KEY": "do-not-print-this-secret"})
        with self.assertRaises(RoomError) as caught:
            self.prepare()
        self.assertIn("ANTHROPIC_API_KEY", str(caught.exception))
        self.assertNotIn("do-not-print-this-secret", str(caught.exception))
        self.env(self.exact())
        (self.profile_b / "settings.json").write_text(json.dumps({"apiKeyHelper": "/fixture/helper"}))
        with self.assertRaises(RoomError) as caught:
            self.prepare()
        self.assertIn("apiKeyHelper", str(caught.exception))
        (self.profile_b / "settings.json").unlink()
        (self.profile_b / ".credentials.json").write_text("{}")
        self.assertRaises(RoomError, self.prepare)
        (self.profile_b / ".credentials.json").unlink()
        with patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "do-not-print-this-secret"}):
            with self.assertRaises(RoomError) as caught:
                self.prepare()
            self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", str(caught.exception))
            self.assertNotIn("do-not-print-this-secret", str(caught.exception))
        self.prepare()  # clean sources pass again

    def test_default_pin_refuses_any_env_key(self):
        self.room = self.open_pinned("default", feature="default-room")
        self.service.ao_room_spec_put(self.room, 1, "Implement the exact test contract.", self.gates,
                                      "Astra approves this scope")
        self.env({"CLAUDE_CONFIG_DIR": str(self.claude_env)})
        self.assertRaises(RoomError, self.prepare)
        self.env()
        self.prepare()

    def test_bind_refuses_after_env_drift(self):
        self.env(self.exact())
        self.prepare()
        self.env({"CLAUDE_CONFIG_DIR": "/drifted"})
        self.assertRaises(RoomError, self.service.ao_room_bind, self.room, "engineer", "engineer",
                          ao_workflow.FABLE_MODEL, "max")

    def test_send_refuses_after_env_drift_without_intent_or_post(self):
        self.env(self.exact())
        self.bind()
        self.env({"CLAUDE_CONFIG_DIR": "/drifted"})
        posts = len(self.fake.posts)
        self.assertRaises(RoomError, self.service.ao_room_send, self.room, "engineer",
                          "Perform the exact authorized purpose.", "spec_review", purpose="spec_review")
        self.assertEqual(self.state()["requests"], {})
        self.assertEqual(len(self.fake.posts), posts)

    def test_send_refuses_after_identity_change(self):
        self.env(self.exact())
        self.bind()
        (self.profile_b / ".claude.json").write_text(json.dumps(
            {"oauthAccount": {"accountUuid": "other-account-uuid"}}))
        with self.assertRaises(RoomError) as caught:
            self.service.ao_room_send(self.room, "engineer", "Perform the exact authorized purpose.",
                                      "spec_review", purpose="spec_review")
        self.assertIn("re-enroll", str(caught.exception))
        self.assertEqual(self.state()["requests"], {})

    def test_send_records_attribution(self):
        self.env(self.exact())
        self.bind()
        self.send("spec_review")
        request = self.state()["requests"]["spec_review"]
        self.assertEqual(request["account_profile"], {"name": "b", "identity_digest": B_DIGEST})
        listed = {item["request_id"]: item for item in self.service.ao_room_status(self.room)["requests"]}
        self.assertEqual(listed["spec_review"]["account_profile"],
                         {"name": "b", "identity_digest": B_DIGEST})

    def test_sync_observes_drift_without_refusing(self):
        self.env(self.exact())
        self.bind()
        self.env({"CLAUDE_CONFIG_DIR": "/drifted"})
        self.service.ao_room_sync(self.room)
        rules = self.state()["routing_rules"]
        self.assertIs(rules["consistent"], False)
        self.assertIn("CLAUDE_CONFIG_DIR", rules["error"])
        self.assertNotIn("/drifted", rules["error"])
        status = self.service.ao_room_status(self.room)
        self.assertEqual(status["delegate"]["routing"]["status"], "unverified")


class SurfacesTests(AccountFixture):
    def test_deepseek_initialize_carries_pin(self):
        provider = self.root / "provider.json"
        provider.write_text(json.dumps({"api_key_file": str(self.home / "secrets" / "synthetic-key")}))
        config_path = self.home / "config.json"
        value = ao.read(config_path)
        value.update(deepseek_config=str(provider), delegate_provider="deepseek")
        ao.atomic(config_path, value)
        pin = ap.pin(self.home, "b", value)
        directory = self.home / "ao" / "rooms" / "ao-deepseek-pin"
        directory.mkdir(parents=True, mode=0o700)
        state = {"version": 2, "room_id": "ao-deepseek-pin", "project_path": str(self.repo),
                 "account_profile": pin}
        ao_delegates.initialize(self.service, directory, state, "deepseek")
        settings = ao.read(directory / "settings.json")
        self.assertEqual(settings["claude_config_dir"], str(self.profile_b))
        self.assertEqual(settings["claude_config_dir_override"], str(self.profile_b))
        self.assertEqual(settings["account_profile"]["name"], "b")
        implementation = ao.read(directory / "profiles" / "implementation.json")
        self.assertEqual(implementation["claude_config_dir"], str(self.profile_b))
        self.assertEqual(implementation["claude_config_dir_override"], str(self.profile_b))

    def test_mcp_schema_and_call_dispatch(self):
        properties = project_room.TOOL_SCHEMAS["ao_room_open"][1]["properties"]
        self.assertIn("account_profile", properties)
        real = ao.Service
        with patch.object(ao, "Service", lambda home, factory=None: real(home, lambda url: self.fake)):
            result = project_room.Service(str(self.home)).call(
                "ao_room_open", {"project_path": str(self.repo), "feature": "call-room",
                                 "ao_project_id": "project", "authorization": "User authorized this feature",
                                 "ao_url": "http://127.0.0.1:1234", "delegate_provider": "none",
                                 "account_profile": "b"})
        self.assertEqual(result["account_profile"]["name"], "b")

    def test_cli_setup_enrolls_profile(self):
        profile_c = self.root / "profile-c"
        profile_c.mkdir(mode=0o700)
        (profile_c / ".claude.json").write_text(json.dumps(
            {"oauthAccount": {"accountUuid": "c-account-uuid"}}))
        output = io.StringIO()
        with patch.dict(os.environ):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            with contextlib.redirect_stdout(output):
                code = project_room.main(["--home", str(self.home), "setup",
                                          "--claude-profile", "c", "--config-dir", str(profile_c)])
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["enrolled_profile"], "c")
        self.assertEqual(result["claude_profiles"], ["b", "c"])

    def test_usage_audit_work_unit_carries_profile(self):
        unit = ao_usage_audit._work_unit({"role": "engineer", "purpose": "spec_review", "state": "sent",
                                          "provider_epoch": 0,
                                          "account_profile": {"name": "b", "identity_digest": B_DIGEST}})
        self.assertEqual(unit["account_profile"], {"name": "b", "identity_digest": B_DIGEST})
        self.assertIsNone(ao_usage_audit._work_unit({})["account_profile"])
        self.assertIsNone(ao_usage_audit._unavailable("fixture")["work_unit"]["account_profile"])


if __name__ == "__main__":
    unittest.main()
