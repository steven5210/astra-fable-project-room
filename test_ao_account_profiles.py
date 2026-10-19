"""Unit tests for ao_account_profiles: fixed Claude account profiles (fixtures only)."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import unittest
from unittest.mock import patch

import ao_account_profiles as ap
import ao_routing
from room import RoomError


EMAIL = "profile-owner@example.invalid"
SECRET = "do-not-print-this-secret"


class Fixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.home = self.root / "user-home"
        self.home.mkdir(mode=0o700)
        self.controller = self.root / "controller"
        self.controller.mkdir(mode=0o700)
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        for key in (*ap.CREDENTIAL_ENV, ap.CUSTOM_HEADERS):
            os.environ.pop(key, None)
        self._home = patch.dict(os.environ, {"HOME": str(self.home)})
        self._home.start()
        self.addCleanup(self._home.stop)
        self.addCleanup(self._tmp.cleanup)

    def profile_dir(self, name="profile-b", uuid="b-account-uuid", email=EMAIL):
        directory = self.root / name
        directory.mkdir(mode=0o700)
        account = {"accountUuid": uuid}
        if email is not None:
            account["emailAddress"] = email
        (directory / ".claude.json").write_text(json.dumps({"oauthAccount": account}))
        return directory

    def default_dir(self):
        directory = self.home / ".claude"
        directory.mkdir(mode=0o700, exist_ok=True)
        (self.home / ".claude.json").write_text(json.dumps(
            {"oauthAccount": {"accountUuid": "default-account-uuid"}}))
        return directory

    def test_profile_name(self):
        self.assertEqual(ap.profile_name("b-9"), "b-9")
        for bad in ("", "B", "-a", "a_", "default", 7, None, "a" * 65):
            with self.subTest(bad=bad):
                self.assertRaises(RoomError, ap.profile_name, bad)

    def test_validate_directory(self):
        good = self.profile_dir()
        self.assertEqual(ap.validate_directory(str(good)), str(good))
        self.assertRaises(RoomError, ap.validate_directory, "")
        self.assertRaises(RoomError, ap.validate_directory, None)
        self.assertRaises(RoomError, ap.validate_directory, "relative/path")
        self.assertRaises(RoomError, ap.validate_directory, "~/x")
        self.assertRaises(RoomError, ap.validate_directory, str(good) + "/")
        self.assertRaises(RoomError, ap.validate_directory, str(good) + "e\u0301")
        self.assertRaises(RoomError, ap.validate_directory, str(self.root) + "/./" + good.name)
        self.assertRaises(RoomError, ap.validate_directory, str(self.root / "missing"))
        loose = self.root / "loose"
        loose.mkdir(mode=0o755)
        self.assertRaises(RoomError, ap.validate_directory, str(loose))
        real = self.root / "real"
        real.mkdir(mode=0o700)
        link = self.root / "link"
        link.symlink_to(real)
        self.assertRaises(RoomError, ap.validate_directory, str(link))
        leaf = self.root / "a-file"
        leaf.write_text("x")
        self.assertRaises(RoomError, ap.validate_directory, str(leaf))

    def test_keychain_service(self):
        self.assertEqual(ap.keychain_service(None), ap.KEYCHAIN_BASE)
        self.assertEqual(ap.keychain_service(""), ap.KEYCHAIN_BASE)
        plain = ap.keychain_service("/x/y")
        self.assertTrue(plain.startswith(ap.KEYCHAIN_BASE + "-"))
        self.assertEqual(plain, ap.KEYCHAIN_BASE + "-" +
                         hashlib.sha256(b"/x/y").hexdigest()[:8])
        self.assertNotEqual(plain, ap.keychain_service("/x/y/"))
        decomposed = "/x/e\u0301y"
        composed = "/x/\u00e9y"
        self.assertEqual(ap.keychain_service(decomposed), ap.keychain_service(composed))

    def test_identity_digest(self):
        directory = self.profile_dir()
        expected = hashlib.sha256(b"b-account-uuid").hexdigest()
        self.assertEqual(ap.identity_digest(directory / ".claude.json"), expected)
        for config in ({}, {"oauthAccount": {}}, {"oauthAccount": {"accountUuid": ""}},
                       {"oauthAccount": {"accountUuid": 7}}, []):
            with self.subTest(config=config):
                (directory / ".claude.json").write_text(json.dumps(config))
                with self.assertRaises(RoomError) as caught:
                    ap.identity_digest(directory / ".claude.json")
                self.assertNotIn("@", str(caught.exception))
                self.assertNotIn(EMAIL, str(caught.exception))
        with self.assertRaises(RoomError) as caught:
            ap.identity_digest(directory / "absent.json")
        self.assertNotIn("@", str(caught.exception))
        (directory / ".claude.json").write_text("{")
        with self.assertRaises(RoomError):
            ap.identity_digest(directory / ".claude.json")

    def test_load(self):
        self.assertEqual(ap.load(self.controller), {"version": 1, "profiles": {}})
        path = self.controller / ap.PROFILES_FILE
        for bad in ("{", "[]", '{"version": 2, "profiles": {}}', '{"version": 1}',
                    '{"version": 1, "profiles": {"default": {}}}',
                    '{"version": 1, "profiles": {"b": {"name": "c"}}}'):
            with self.subTest(bad=bad):
                path.write_text(bad)
                self.assertRaises(RoomError, ap.load, self.controller)
                path.unlink()

    def test_enroll(self):
        self.default_dir()
        directory = self.profile_dir()
        record = ap.enroll(self.controller, "b", str(directory), {})
        self.assertEqual(record["name"], "b")
        self.assertEqual(record["config_dir_exact"], str(directory))
        self.assertEqual(record["identity_digest"], hashlib.sha256(b"b-account-uuid").hexdigest())
        self.assertEqual(record["keychain_service"], ap.keychain_service(str(directory)))
        path = self.controller / ap.PROFILES_FILE
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        stored = json.loads(path.read_text())
        self.assertEqual(stored["version"], 1)
        self.assertEqual(stored["profiles"]["b"]["identity_digest"], record["identity_digest"])
        # Re-enrolling the same name and directory refreshes the record.
        again = ap.enroll(self.controller, "b", str(directory), {})
        self.assertEqual(again["name"], "b")
        # Same name under a different directory refuses.
        other = self.profile_dir("profile-c", uuid="c-account-uuid")
        self.assertRaises(RoomError, ap.enroll, self.controller, "b", str(other), {})
        # One account under two names refuses.
        self.assertRaises(RoomError, ap.enroll, self.controller, "c", str(directory), {})
        # The default directory is not an enrollable profile.
        self.assertRaises(RoomError, ap.enroll, self.controller, "d",
                          str(self.home / ".claude"), {})
        self.assertRaises(RoomError, ap.enroll, self.controller, "d",
                          str(self.home / ".claude"),
                          {"claude_config_dir": str(self.home / ".claude")})
        # A refusal writes no partial enrollment file state.
        with self.assertRaises(RoomError):
            ap.enroll(self.controller, "bad name", str(other), {})

    def test_default_login_dedup(self):
        self.default_dir()  # saved default login uuid "default-account-uuid"
        duplicate = self.profile_dir("profile-dup", uuid="default-account-uuid", email=None)
        with self.assertRaises(RoomError) as caught:
            ap.enroll(self.controller, "dup", str(duplicate), {})
        self.assertIn("default", str(caught.exception))
        different = self.profile_dir("profile-e", uuid="e-account-uuid", email=None)
        record = ap.enroll(self.controller, "e", str(different), {})
        self.assertIs(record["default_identity_compared"], True)
        (self.home / ".claude.json").unlink()
        another = self.profile_dir("profile-f", uuid="f-account-uuid", email=None)
        record = ap.enroll(self.controller, "f", str(another), {})
        self.assertIs(record["default_identity_compared"], False)

    def test_default_pin_refuses_when_default_login_is_enrolled(self):
        self.default_dir()
        directory = self.profile_dir()
        ap.enroll(self.controller, "b", str(directory), {})
        (self.home / ".claude.json").write_text(json.dumps(
            {"oauthAccount": {"accountUuid": "b-account-uuid"}}))
        with self.assertRaises(RoomError) as caught:
            ap.pin(self.controller, "default", {})
        self.assertIn("b", str(caught.exception))
        self.assertIn("re-enroll", str(caught.exception))
        self.default_dir()
        self.assertEqual(ap.pin(self.controller, "default", {})["name"], "default")

    def test_pin(self):
        self.default_dir()
        directory = self.profile_dir()
        record = ap.enroll(self.controller, "b", str(directory), {})
        pin = ap.pin(self.controller, "b", {})
        self.assertEqual(pin["version"], 1)
        self.assertEqual(pin["name"], "b")
        self.assertEqual(pin["config_dir_exact"], str(directory))
        self.assertEqual(pin["claude_config_dir"], str(directory))
        self.assertEqual(pin["global_config_path"], str(directory / ".claude.json"))
        self.assertEqual(pin["identity_digest"], record["identity_digest"])
        self.assertEqual(pin["keychain_service"], record["keychain_service"])
        self.assertEqual(pin["enrolled_at"], record["enrolled_at"])
        self.assertIn("identity_digest", pin["meaning"])
        # Default pin resolves the HOME-backed saved login.
        default = ap.pin(self.controller, "default", {})
        self.assertEqual(default["config_dir_exact"], None)
        self.assertEqual(default["claude_config_dir"], str((self.home / ".claude").resolve()))
        self.assertEqual(default["global_config_path"], str(self.home / ".claude.json"))
        self.assertEqual(default["keychain_service"], ap.KEYCHAIN_BASE)
        self.assertEqual(default["enrolled_at"], None)
        # Default refuses while the controller records an explicit override or directory.
        self.assertRaises(RoomError, ap.pin, self.controller, "default",
                          {"claude_config_dir_override": str(directory)})
        self.assertRaises(RoomError, ap.pin, self.controller, "default",
                          {"claude_config_dir": str(directory)})
        # Unknown and identity-drifted profiles refuse.
        self.assertRaises(RoomError, ap.pin, self.controller, "unknown", {})
        (directory / ".claude.json").write_text(json.dumps(
            {"oauthAccount": {"accountUuid": "other-account-uuid"}}))
        with self.assertRaises(RoomError) as caught:
            ap.pin(self.controller, "b", {})
        self.assertIn("re-enroll", str(caught.exception))

    def test_check_project_env(self):
        directory = self.profile_dir()
        pin = {"name": "b", "config_dir_exact": str(directory)}
        ap.check_project_env({"CLAUDE_CONFIG_DIR": str(directory)}, pin)
        for env in ({}, {"CLAUDE_CONFIG_DIR": str(directory) + "/"},
                    {"CLAUDE_CONFIG_DIR": "/elsewhere"}, {"CLAUDE_CONFIG_DIR": 1}):
            with self.subTest(env=env):
                with self.assertRaises(RoomError) as caught:
                    ap.check_project_env(env, pin)
                self.assertNotIn(SECRET, str(caught.exception))
        default = {"name": "default", "config_dir_exact": None}
        ap.check_project_env({}, default)
        self.assertRaises(RoomError, ap.check_project_env,
                          {"CLAUDE_CONFIG_DIR": str(directory)}, default)

    def test_check_credential_env(self):
        for key in ap.CREDENTIAL_ENV:
            with self.subTest(key=key):
                with self.assertRaises(RoomError) as caught:
                    ap.check_credential_env({key: SECRET}, "fixture environment")
                text = str(caught.exception)
                self.assertIn(key, text)
                self.assertNotIn(SECRET, text)
        clean = {key: "" for key in ap.CREDENTIAL_ENV}
        del clean["CLAUDE_SECURESTORAGE_CONFIG_DIR"]
        ap.check_credential_env(clean, "fixture environment")
        with self.assertRaises(RoomError) as caught:
            ap.check_credential_env({"CLAUDE_SECURESTORAGE_CONFIG_DIR": ""}, "fixture environment")
        self.assertIn("CLAUDE_SECURESTORAGE_CONFIG_DIR", str(caught.exception))
        self.assertIn("present", str(caught.exception))
        with self.assertRaises(RoomError):
            ap.check_credential_env({"CLAUDE_SECURESTORAGE_CONFIG_DIR": "/somewhere"},
                                    "fixture environment")
        with self.assertRaises(RoomError) as caught:
            ap.check_credential_env({ap.CUSTOM_HEADERS: "X: 1\n authorization : " + SECRET},
                                    "fixture environment")
        self.assertIn(ap.CUSTOM_HEADERS, str(caught.exception))
        self.assertNotIn(SECRET, str(caught.exception))
        ap.check_credential_env({ap.CUSTOM_HEADERS: "X-Trace: 1"}, "fixture environment")
        self.assertRaises(RoomError, ap.check_credential_env, "not-a-mapping", "fixture")

    def _pin(self, directory):
        return {"name": "b", "config_dir_exact": str(directory),
                "claude_config_dir": str(directory),
                "global_config_path": str(directory / ".claude.json"),
                "identity_digest": ap.identity_digest(directory / ".claude.json")}

    def test_check_credential_settings(self):
        worktree = self.root / "worktree"
        worktree.mkdir()
        directory = self.profile_dir()
        pin = self._pin(directory)
        environ = {}
        ap.check_credential_settings(pin, worktree, environ)
        # user source
        (directory / "settings.json").write_text(json.dumps({ap.HELPER_KEY: "/x"}))
        self.assertRaises(RoomError, ap.check_credential_settings, pin, worktree, environ)
        (directory / "settings.json").write_text(json.dumps({"env": {"ANTHROPIC_API_KEY": SECRET}}))
        with self.assertRaises(RoomError) as caught:
            ap.check_credential_settings(pin, worktree, environ)
        self.assertIn("ANTHROPIC_API_KEY", str(caught.exception))
        self.assertNotIn(SECRET, str(caught.exception))
        (directory / "settings.json").unlink()
        # managed source
        managed_file = self.root / "managed-settings.json"
        managed_file.write_text(json.dumps({ap.HELPER_KEY: "/x"}))
        managed_env = {"CLAUDE_CODE_MANAGED_SETTINGS_PATH": str(managed_file)}
        self.assertEqual(ao_routing.managed_settings_path(managed_env), managed_file)
        self.assertRaises(RoomError, ap.check_credential_settings, pin, worktree, managed_env)
        managed_file.write_text("{}")
        ap.check_credential_settings(pin, worktree, managed_env)
        # managed drop-in directory: helper, credential env, malformed JSON and non-json files
        dropins = managed_file.parent / "managed-settings.d"
        dropins.mkdir()
        (dropins / "notes.txt").write_text("ignored")
        (dropins / "50-helper.json").write_text(json.dumps({ap.HELPER_KEY: "/x"}))
        with self.assertRaises(RoomError) as caught:
            ap.check_credential_settings(pin, worktree, managed_env)
        self.assertIn("managed drop-in", str(caught.exception))
        (dropins / "50-helper.json").write_text(json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": SECRET}}))
        with self.assertRaises(RoomError) as caught:
            ap.check_credential_settings(pin, worktree, managed_env)
        self.assertIn("ANTHROPIC_AUTH_TOKEN", str(caught.exception))
        self.assertNotIn(SECRET, str(caught.exception))
        (dropins / "50-helper.json").write_text("{")
        self.assertRaises(RoomError, ap.check_credential_settings, pin, worktree, managed_env)
        (dropins / "50-helper.json").unlink()
        ap.check_credential_settings(pin, worktree, managed_env)
        shutil.rmtree(dropins)
        ap.check_credential_settings(pin, worktree, managed_env)
        dropins.write_text("not a directory")
        self.assertRaises(RoomError, ap.check_credential_settings, pin, worktree, managed_env)
        dropins.unlink()
        # project source
        nested = worktree / ".claude"
        nested.mkdir()
        (nested / "settings.json").write_text(json.dumps({ap.HELPER_KEY: "/x"}))
        self.assertRaises(RoomError, ap.check_credential_settings, pin, worktree, environ)
        (nested / "settings.json").unlink()
        # local source
        (nested / "settings.local.json").write_text(json.dumps({ap.HELPER_KEY: "/x"}))
        self.assertRaises(RoomError, ap.check_credential_settings, pin, worktree, environ)
        (nested / "settings.local.json").unlink()
        # .credentials.json beside the profile: file and dangling symlink both refuse.
        credentials = directory / ap.CREDENTIALS_FILE
        credentials.write_text("{}")
        self.assertRaises(RoomError, ap.check_credential_settings, pin, worktree, environ)
        credentials.unlink()
        credentials.symlink_to(directory / "absent-target")
        self.assertRaises(RoomError, ap.check_credential_settings, pin, worktree, environ)
        credentials.unlink()
        ap.check_credential_settings(pin, worktree, environ)

    def test_check_identity_and_context(self):
        directory = self.profile_dir()
        pin = self._pin(directory)
        ap.check_identity(pin)
        (directory / ".claude.json").write_text(json.dumps(
            {"oauthAccount": {"accountUuid": "other-account-uuid"}}))
        with self.assertRaises(RoomError) as caught:
            ap.check_identity(pin)
        self.assertIn("re-enroll", str(caught.exception))
        good = {"claude_config_dir": pin["claude_config_dir"],
                "claude_config_dir_override": pin["config_dir_exact"]}
        ap.check_context(good, pin)
        self.assertRaises(RoomError, ap.check_context,
                          {**good, "claude_config_dir": "/other"}, pin)
        self.assertRaises(RoomError, ap.check_context,
                          {**good, "claude_config_dir_override": None}, pin)
        self.assertRaises(RoomError, ap.check_context, {}, pin)

    def test_trust_status(self):
        worktree = self.root / "worktree"
        directory = self.profile_dir()
        pin = self._pin(directory)
        status = ap.trust_status(pin, worktree, "/bin/claude")
        self.assertIs(status["trust_recorded"], False)
        self.assertEqual(status["explicit_setup"]["cwd"], str(worktree))
        self.assertEqual(status["explicit_setup"]["env"], {"CLAUDE_CONFIG_DIR": str(directory)})
        self.assertEqual(status["explicit_setup"]["argv"], ["/bin/claude"])
        config = json.loads((directory / ".claude.json").read_text())
        original = (directory / ".claude.json").read_bytes()
        config["projects"] = {str(worktree): {"hasTrustDialogAccepted": True}}
        (directory / ".claude.json").write_text(json.dumps(config))
        self.assertIs(ap.trust_status(pin, worktree, "/bin/claude")["trust_recorded"], True)
        # trust_status never writes to the global config.
        self.assertEqual((directory / ".claude.json").read_text(), json.dumps(config))
        (directory / ".claude.json").unlink()
        self.assertIs(ap.trust_status(pin, worktree, "/bin/claude")["trust_recorded"], None)
        default = {"name": "default", "config_dir_exact": None,
                   "global_config_path": str(self.home / ".claude.json")}
        self.default_dir()
        status = ap.trust_status(default, worktree, "/bin/claude")
        self.assertEqual(status["explicit_setup"]["env"], {})
        self.assertTrue(original.startswith(b"{"))


if __name__ == "__main__":
    unittest.main()
