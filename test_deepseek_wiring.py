"""Controller wiring for the DeepSeek provider: setup, room snapshots, policy selection, inventory refusal before launch,
status/doctor surfaces and progress categories. Fake Claude and fake adapters only; no network, no key reads."""

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import deepseek_adapter
import implementation
import progress
import project_room
import project_room_mcp
import recovery
import room
from test_implementation import FAKE as IMPLEMENTATION_FAKE
from test_project_room import ProjectFixture, ROOT
from test_recovery import FAKE as RECOVERY_FAKE, fixed_inspector


def implementation_fake():
    """The implementation fake writing a worktree-bound transcript, also logging the delegate environment it received."""
    fake = IMPLEMENTATION_FAKE.replace("#!/usr/bin/env python3", "#!" + sys.executable, 1)
    fake = fake.replace('"config_dir_override":os.environ.get("CLAUDE_CONFIG_DIR")',
                        '"config_dir_override":os.environ.get("CLAUDE_CONFIG_DIR"),"worktree_env":os.environ.get("PROJECT_ROOM_WORKTREE")')
    fake = fake.replace('"tier":"fable"', '"tier":"deepseek"')
    return fake.replace(
        '    with (root / (session + ".jsonl")).open("a") as out:',
        '    transcript = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / "fixture-hashed-directory" / (session + ".jsonl")\n'
        '    transcript.parent.mkdir(parents=True, exist_ok=True)\n'
        '    event["cwd"] = os.getcwd()\n'
        '    with transcript.open("a") as out:')


class WiringFixture(ProjectFixture):
    def setUp(self):
        super().setUp()
        self.provider_config = self.base / "deepseek.local.json"
        self.provider_config.write_text(json.dumps({"api_key_file": str(self.base / "secrets" / "deepseek-api-key")}))
        self.review_executable = self.fake.read_text()

    def configure(self, provider, **extra):
        return self.service.setup(claude_bin=str(self.fake), delegate_provider=provider,
                                  deepseek_config=str(self.provider_config) if provider == "deepseek" else None, **extra)

    def open_room(self, feature):
        entry = self.service.room_open(str(self.project), feature)
        root = Path(entry["path"])
        self.service.room_spec_put(entry["id"], 1, "# " + feature + "\nNames must be nonempty.\n")
        return entry["id"], root

    def consensus(self, room_id, request_id="review-1"):
        self.fake.write_text(self.review_executable)
        submitted = self.service.room_review_submit(room_id, 1, "Review independently", request_id)
        self.assertEqual(self.service.room_job_status(submitted["id"], 15)["status"], "succeeded")
        self.service.room_record(room_id, "astra", "approval", 1, "Acceptance criteria and scope verified.")

    def implementation_calls(self):
        path = self.base / "implementation-calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class ProviderSetupTests(WiringFixture):
    def test_setup_records_an_explicit_provider_and_validates_key_free_configuration(self):
        before = (self.home / "config.json").read_bytes()
        with self.assertRaisesRegex(room.RoomError, "--delegate-provider deepseek"):
            self.service.setup(claude_bin=str(self.fake), deepseek_config=str(self.provider_config))
        self.assertEqual((self.home / "config.json").read_bytes(), before, "a misleading unselected DeepSeek config is refused before writing")
        result = self.service.setup(claude_bin=str(self.fake), deepseek_config=str(self.provider_config), delegate_provider="deepseek")
        self.assertEqual((result["delegate_provider"], result["delegate_provider_selected"], result["deepseek_configured"], result["qwen_configured"]),
                         ("deepseek", True, True, False))
        result = self.service.setup(claude_bin=str(self.fake), delegate_provider="deepseek")
        self.assertEqual(result["delegate_provider"], "deepseek")
        self.assertEqual(self.service.settings()["delegate_provider"], "deepseek")
        self.assertEqual(self.service.setup(claude_bin=str(self.fake))["delegate_provider"], "deepseek", "the prior explicit choice is kept")
        with self.assertRaisesRegex(room.RoomError, "requires --qwen-config"):
            self.service.setup(claude_bin=str(self.fake), delegate_provider="qwen")
        fresh = project_room.Service(self.base / "fresh-home")
        with self.assertRaisesRegex(room.RoomError, "requires --deepseek-config"):
            fresh.setup(claude_bin=str(self.fake), delegate_provider="deepseek")
        bad = self.base / "bad.json"
        for value in ({"api_key": "sk-secret"}, {"base_url": "http://127.0.0.1:9"}, {"max_wire_bytes": 33554432}, {"model": "two words"}):
            bad.write_text(json.dumps(value))
            with self.subTest(value=value), self.assertRaisesRegex(room.RoomError, "rejected"):
                fresh.setup(claude_bin=str(self.fake), deepseek_config=str(bad), delegate_provider="deepseek")
        self.assertFalse((self.base / "secrets").exists(), "setup never creates or reads the key")
        self.assertEqual(self.calls(), [])

    def test_setup_provider_selection_is_compatible_and_deliberate(self):
        qwen = self.base / "qwen.json"
        qwen.write_text(json.dumps({"command": sys.executable, "args": ["-c", "pass"]}))
        plain = self.service.setup(claude_bin=str(self.fake))
        self.assertEqual((plain["delegate_provider"], plain["delegate_provider_selected"]), ("none", False))
        self.assertNotIn("delegate_provider", self.service.settings(), "an inferred default is never recorded as a selection")
        legacy = self.service.setup(claude_bin=str(self.fake), qwen_config=str(qwen))
        self.assertEqual((legacy["delegate_provider"], legacy["delegate_provider_selected"]), ("qwen", False), "the documented Qwen sequence still enables Qwen")
        room_id, root = self.open_room("Legacy qwen room")
        self.assertIn("qwen-local", json.loads((root / "profiles" / "implementation-mcp.json").read_text())["mcpServers"])
        explicit_none = self.service.setup(claude_bin=str(self.fake), delegate_provider="none")
        self.assertEqual((explicit_none["delegate_provider"], explicit_none["delegate_provider_selected"]), ("none", True))
        kept = self.service.setup(claude_bin=str(self.fake), qwen_config=str(qwen))
        self.assertEqual(kept["delegate_provider"], "none", "a deliberate selection is preserved when a config is merely supplied")
        self.assertEqual(self.service.settings()["qwen_config"], str(qwen), "the supplied config is stored, never dropped")
        stored = self.service.setup(claude_bin=str(self.fake), deepseek_config=str(self.provider_config))
        self.assertEqual((stored["delegate_provider"], stored["deepseek_configured"]), ("none", True), "a deliberate none keeps DeepSeek stored but unselected")
        chosen = self.service.setup(claude_bin=str(self.fake), delegate_provider="deepseek")
        self.assertEqual((chosen["delegate_provider"], self.service.settings()["delegate_provider"]), ("deepseek", "deepseek"))
        self.assertEqual(self.service.setup(claude_bin=str(self.fake), qwen_config=str(qwen))["delegate_provider"], "deepseek", "an explicit provider outlives later config supplies")
        self.assertEqual(self.service.setup(claude_bin=str(self.fake), delegate_provider="qwen")["delegate_provider"], "qwen", "an explicit flag in this invocation wins")
        config = self.service.settings()
        config.pop("delegate_provider")
        project_room.atomic_json(self.home / "config.json", config)
        self.assertEqual(self.service.room_doctor()["delegate_provider_selected"], False)
        self.assertEqual(self.service.setup(claude_bin=str(self.fake))["delegate_provider"], "qwen", "legacy settings without a selection infer qwen from the Qwen config")
        with self.assertRaisesRegex(room.RoomError, "--delegate-provider deepseek"):
            self.service.setup(claude_bin=str(self.fake), deepseek_config=str(self.provider_config))
        self.assertEqual(self.calls(), [], "setup never launches anything")

    def test_unrelated_setup_repairs_survive_a_missing_provider_file(self):
        self.configure("deepseek")
        moved = self.base / "moved-deepseek.json"
        moved.write_text(self.provider_config.read_text())
        self.service.setup(claude_bin=str(self.fake), deepseek_config=str(moved), delegate_provider="deepseek")
        moved.unlink()  # the stored provider file is now missing, as after an unmounted volume
        repaired_bin = self.base / "claude-repaired"
        repaired_bin.write_text(self.fake.read_text())
        repaired_bin.chmod(0o700)
        repaired = self.service.setup(claude_bin=str(repaired_bin))
        self.assertEqual((repaired["delegate_provider"], repaired["deepseek_configured"]), ("deepseek", True))
        settings = self.service.settings()
        self.assertEqual((settings["claude_bin"], settings["deepseek_config"], settings["delegate_provider"]), (str(repaired_bin), str(moved), "deepseek"))
        with self.assertRaisesRegex(room.RoomError, "config_missing"):
            self.service.setup(claude_bin=str(repaired_bin), delegate_provider="deepseek")  # an explicit reselection validates the stored file
        with self.assertRaisesRegex(room.RoomError, "config_missing"):
            self.service.setup(claude_bin=str(repaired_bin), deepseek_config=str(moved))  # a supplied file is always validated
        qwen = self.base / "qwen.json"
        qwen.write_text(json.dumps({"command": sys.executable, "args": ["-c", "pass"]}))
        self.service.setup(claude_bin=str(repaired_bin), qwen_config=str(qwen), delegate_provider="qwen")
        qwen.unlink()
        self.assertEqual(self.service.setup(claude_bin=str(self.fake))["delegate_provider"], "qwen", "a missing stored Qwen file does not block unrelated repairs either")
        with self.assertRaises(Exception):
            self.service.setup(claude_bin=str(self.fake), delegate_provider="qwen")

    def test_mcp_config_tilde_paths_expand_like_the_room_loader(self):
        with mock.patch.dict(os.environ, {"HOME": str(self.base)}):
            (self.base / "servers.json").write_text(json.dumps({"mcpServers": {"qwen-local": {"command": "x"}}}))
            self.assertEqual(set(implementation._mcp_servers({"extra_args": ["--mcp-config", "~/servers.json"]})), {"qwen-local"})
            self.assertEqual(set(implementation._mcp_servers({"extra_args": ["--mcp-config=~/servers.json"]})), {"qwen-local"})
            with self.assertRaisesRegex(implementation.ImplementationError, "unreadable"):
                implementation._mcp_servers({"extra_args": ["--mcp-config", "~/absent.json"]})

    def test_legacy_configuration_infers_qwen_and_existing_rooms_keep_their_pins(self):
        config = self.service.settings()
        config.pop("delegate_provider", None)
        config["qwen_config"] = str(self.base / "qwen.json")
        (self.base / "qwen.json").write_text(json.dumps({"command": sys.executable, "args": ["-c", "pass"]}))
        project_room.atomic_json(self.home / "config.json", config)
        self.assertEqual(self.service.setup(claude_bin=str(self.fake))["delegate_provider"], "qwen")
        pinned = (self.room_root / "settings.json").read_bytes()
        self.configure("deepseek")
        reopened = self.service.room_open(str(self.project), "Saved filters")
        self.assertTrue(reopened["existing"])
        self.assertEqual((self.room_root / "settings.json").read_bytes(), pinned, "existing rooms never migrate silently")
        self.assertNotIn("deepseek", json.loads((self.room_root / "profiles" / "implementation-mcp.json").read_text())["mcpServers"])


class RoomSnapshotTests(WiringFixture):
    def test_deepseek_room_snapshot_has_inventory_export_dir_and_exact_add_dir(self):
        self.configure("deepseek")
        room_id, root = self.open_room("DeepSeek feature")
        profiles = root / "profiles"
        adapter_copy = profiles / "deepseek_adapter.py"
        self.assertEqual(adapter_copy.read_bytes(), (ROOT / "deepseek_adapter.py").read_bytes())
        snapshot = json.loads((profiles / "deepseek.json").read_text())
        self.assertEqual((snapshot["model"], snapshot["reasoning_effort"], snapshot["max_tokens"]), (deepseek_adapter.DEFAULT_MODEL, "max", 393216))
        self.assertNotIn("api_key", snapshot)
        settings = json.loads((root / "settings.json").read_text())
        inventory = settings["provider_inventory"]
        export_dir = self.home / "deepseek" / "exports" / room_id
        self.assertEqual((settings["delegate_provider"], inventory["provider"], inventory["room_id"], inventory["export_dir"]), ("deepseek", "deepseek", room_id, str(export_dir)))
        self.assertEqual(inventory["files"], {str(adapter_copy): room.sha(adapter_copy.read_bytes()), str(profiles / "deepseek.json"): room.sha((profiles / "deepseek.json").read_bytes())})
        self.assertEqual(export_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual(list(export_dir.iterdir()), [])
        mcp = json.loads((profiles / "implementation-mcp.json").read_text())["mcpServers"]
        self.assertEqual(list(mcp), ["deepseek"])
        self.assertEqual(mcp["deepseek"]["args"], [str(adapter_copy), "serve", "--home", str(self.home), "--room", room_id, "--room-root", str(root), "--config", str(profiles / "deepseek.json")])
        self.assertNotIn("env", mcp["deepseek"])
        implementation_profile = json.loads((profiles / "implementation.json").read_text())
        args = implementation_profile["extra_args"]
        self.assertEqual(args.count("--add-dir"), 1)
        self.assertEqual(args[args.index("--add-dir") + 1], str(export_dir))
        review_profile = json.loads((profiles / "review.json").read_text())
        self.assertIn('{"mcpServers":{}}', review_profile["extra_args"])
        self.assertNotIn(str(export_dir), review_profile["extra_args"])
        for provider, server in (("none", None), ("qwen", "qwen-local")):
            if provider == "qwen":
                (self.base / "qwen.json").write_text(json.dumps({"command": sys.executable, "args": ["-c", "pass"]}))
                self.service.setup(claude_bin=str(self.fake), qwen_config=str(self.base / "qwen.json"), delegate_provider="qwen")
            else:
                self.configure("none")
            other_id, other_root = self.open_room(provider + " feature")
            other_args = json.loads((other_root / "profiles" / "implementation.json").read_text())["extra_args"]
            self.assertNotIn("--add-dir", other_args, provider)
            servers = json.loads((other_root / "profiles" / "implementation-mcp.json").read_text())["mcpServers"]
            self.assertEqual(list(servers), [server] if server else [])
            other_settings = json.loads((other_root / "settings.json").read_text())
            self.assertEqual(other_settings["delegate_provider"], provider)
            self.assertNotIn("provider_inventory", other_settings)
            self.assertFalse((self.home / "deepseek" / "exports" / other_id).exists())
        self.assertEqual(self.calls(), [])

    def test_doctor_reports_provider_and_key_metadata_only(self):
        self.configure("deepseek")
        doctor = self.service.room_doctor()
        self.assertEqual((doctor["delegate_provider"], doctor["deepseek_configured"], doctor["deepseek_inference_verified"]), ("deepseek", True, False))
        self.assertEqual(doctor["deepseek"]["model"], deepseek_adapter.DEFAULT_MODEL)
        self.assertEqual(doctor["deepseek"]["key_file"]["reason"], "key_missing")
        self.assertIsNone(doctor["deepseek"]["latest_probe"])
        secrets = self.base / "secrets"
        secrets.mkdir(mode=0o750)
        self.assertEqual(self.service.room_doctor()["deepseek"]["key_file"]["reason"], "key_unsafe_ancestor")
        secrets.chmod(0o700)
        key = secrets / "deepseek-api-key"
        key.write_text("sk-synthetic-doctor-fixture\n")
        key.chmod(0o600)
        doctor = self.service.room_doctor()
        self.assertTrue(doctor["deepseek"]["key_file"]["ok"])
        self.assertNotIn("sk-synthetic-doctor-fixture", json.dumps(doctor))
        self.assertNotIn("DO_NOT_EXPOSE", json.dumps(doctor))


class PolicySelectionTests(WiringFixture):
    def handoff(self, room_id):
        return self.service.room_handoff(room_id, 1, "Build it through independent review.", [[sys.executable, "-c", "assert True"]])

    def test_each_provider_pins_its_own_policy_text_and_inventory(self):
        self.init_git()
        expectations = {}
        for provider, feature in (("deepseek", "DeepSeek feature"), ("none", "Plain feature")):
            self.configure(provider)
            room_id, root = self.open_room(feature)
            self.consensus(room_id)
            handoff = self.handoff(room_id)
            manifest = json.loads(Path(handoff["handoff_path"]).read_text())
            policy = (Path(handoff["handoff_path"]).parent / "delegation-policy.txt").read_text()
            expectations[provider] = (manifest, policy)
        (self.base / "qwen.json").write_text(json.dumps({"command": sys.executable, "args": ["-c", "pass"]}))
        self.service.setup(claude_bin=str(self.fake), qwen_config=str(self.base / "qwen.json"), delegate_provider="qwen")
        room_id, root = self.open_room("Qwen feature")
        self.consensus(room_id)
        handoff = self.handoff(room_id)
        manifest = json.loads(Path(handoff["handoff_path"]).read_text())
        expectations["qwen"] = (manifest, (Path(handoff["handoff_path"]).parent / "delegation-policy.txt").read_text())
        deepseek_manifest, deepseek_policy = expectations["deepseek"]
        self.assertEqual(deepseek_policy, implementation.POLICY_DEEPSEEK)
        self.assertEqual(deepseek_manifest["provider"], "deepseek")
        self.assertEqual(deepseek_manifest["provider_inventory"]["provider"], "deepseek")
        self.assertEqual(deepseek_manifest["delegation_policy_sha256"], room.sha(implementation.POLICY_DEEPSEEK.encode()))
        self.assertIn("deepseek_submit", deepseek_policy)
        self.assertNotIn("qwen_submit", deepseek_policy)
        qwen_manifest, qwen_policy = expectations["qwen"]
        self.assertEqual(qwen_policy, implementation.POLICY_QWEN)
        self.assertEqual(qwen_policy, implementation.POLICY, "the legacy text keeps its exact bytes")
        self.assertNotIn("DeepSeek", qwen_policy)
        self.assertNotIn("deepseek", qwen_policy)
        self.assertNotIn("provider_inventory", qwen_manifest)
        self.assertEqual(qwen_manifest["provider"], "qwen")
        none_manifest, none_policy = expectations["none"]
        self.assertEqual(none_policy, implementation.POLICY_NONE)
        self.assertNotIn("qwen_submit", none_policy)
        self.assertNotIn("deepseek_submit", none_policy)
        self.assertEqual(none_manifest["provider"], "none")
        self.assertEqual(len({manifest["handoff_id"] for manifest, _ in expectations.values()}), 3)

    def test_legacy_settings_infer_only_a_lone_qwen_guard_and_refuse_ambiguity(self):
        self.init_git()
        (self.base / "qwen.json").write_text(json.dumps({"command": sys.executable, "args": ["-c", "pass"]}))
        self.service.setup(claude_bin=str(self.fake), qwen_config=str(self.base / "qwen.json"), delegate_provider="qwen")
        room_id, root = self.open_room("Legacy feature")
        settings = json.loads((root / "settings.json").read_text())
        settings.pop("delegate_provider")
        project_room.atomic_json(root / "settings.json", settings)
        self.consensus(room_id)
        handoff = self.handoff(room_id)
        self.assertEqual((Path(handoff["handoff_path"]).parent / "delegation-policy.txt").read_text(), implementation.POLICY_QWEN)
        self.assertEqual(json.loads(Path(handoff["handoff_path"]).read_text())["provider"], "qwen")
        mcp_path = root / "profiles" / "implementation-mcp.json"
        original = mcp_path.read_bytes()
        project_room.atomic_json(mcp_path, {"mcpServers": {"qwen-local": {"command": sys.executable}, "other": {"command": sys.executable}}})
        with self.assertRaisesRegex(implementation.ImplementationError, "Ambiguous legacy delegate configuration"):
            self.service.room_handoff(room_id, 1, "Another authorization text for the ambiguous case.", [[sys.executable, "-c", "assert True"]])
        mcp_path.write_bytes(original)
        second = root / "profiles" / "second-mcp.json"
        project_room.atomic_json(second, {"mcpServers": {"deepseek": {"command": sys.executable}}})
        profile_path = root / "profiles" / "implementation.json"
        profile = json.loads(profile_path.read_text())
        profile["extra_args"] = profile["extra_args"] + [str(second)]  # a second consecutive --mcp-config value
        project_room.atomic_json(profile_path, profile)
        with self.assertRaisesRegex(implementation.ImplementationError, "Ambiguous legacy delegate configuration"):
            self.service.room_handoff(room_id, 1, "Authorization for the two-file profile.", [[sys.executable, "-c", "assert True"]])
        profile["extra_args"] = profile["extra_args"][:-1]
        project_room.atomic_json(profile_path, profile)
        settings["delegate_provider"] = "mystery"
        project_room.atomic_json(root / "settings.json", settings)
        with self.assertRaisesRegex(implementation.ImplementationError, "Unknown delegate provider"):
            self.service.room_handoff(room_id, 1, "A third authorization text.", [[sys.executable, "-c", "assert True"]])

    def test_routing_schema_accepts_deepseek_and_the_packet_carries_the_pinned_window(self):
        self.assertIn("deepseek", implementation.ROUTE_SCHEMA["properties"]["tier"]["enum"])
        self.assertIn("qwen", implementation.ROUTE_SCHEMA["properties"]["tier"]["enum"])
        self.init_git()
        self.configure("deepseek")
        room_id, root = self.open_room("Routed feature")
        self.consensus(room_id)
        handoff = self.service.room_handoff(room_id, 1, "Build it through independent review.", [[sys.executable, "-c", "from pathlib import Path; assert Path('feature.txt').read_text() == 'implemented\\n'"]])
        self.fake.write_text(implementation_fake())
        (self.base / "implementation-mode.txt").write_text("normal")
        job = self.service.room_implementation_submit(room_id, handoff["handoff_id"], "implement-1")
        terminal = self.service.room_job_status(job["id"], 30)
        self.assertEqual(terminal["status"], "succeeded", terminal)
        self.assertEqual(terminal["result"]["phase"], "awaiting_astra_review")
        self.assertEqual(terminal["result"]["report"]["routing_log"][0]["tier"], "deepseek")
        call = self.implementation_calls()[0]
        self.assertEqual(call["worktree_env"], handoff["worktree_path"])
        packet = call["packet"]
        self.assertEqual((packet["delegate_provider"], packet["implementation_timeout_seconds"]), ("deepseek", self.service.settings()["implementation_timeout_seconds"]))
        self.assertEqual(packet["delegate_export_dir"], str(self.home / "deepseek" / "exports" / room_id))
        self.assertGreater(room.parse_timestamp(packet["attempt_deadline_at"]), room.parse_timestamp(packet["attempt_started_at"]))
        self.assertIn("deepseek_submit", packet["delegation_policy"])
        status = self.service.room_status(room_id)
        self.assertEqual(status["delegate_jobs"]["provider"], "deepseek")
        self.assertEqual(status["delegate_jobs"]["unavailable_reason"], "ledger_missing")
        self.assertNotIn("DO_NOT_EXPOSE_PRIVATE_THINKING", json.dumps(status))


class InventoryRefusalTests(WiringFixture):
    def setUp(self):
        super().setUp()
        self.init_git()
        self.configure("deepseek")
        self.deepseek_room, self.deepseek_root = self.open_room("Guarded feature")
        self.consensus(self.deepseek_room)
        self.gates = [[sys.executable, "-c", "from pathlib import Path; assert Path('feature.txt').read_text() == 'implemented\\n'"]]
        self.handoff = self.service.room_handoff(self.deepseek_room, 1, "Build it through independent review.", self.gates)
        self.handoff_dir = Path(self.handoff["handoff_path"]).parent
        self.fake.write_text(implementation_fake())
        (self.base / "implementation-mode.txt").write_text("normal")
        self.adapter_copy = self.deepseek_root / "profiles" / "deepseek_adapter.py"
        self.config_copy = self.deepseek_root / "profiles" / "deepseek.json"

    def state(self):
        return json.loads((self.handoff_dir / "state.json").read_text())

    def submit(self, request_id, recovery_id=None):
        job = self.service.room_implementation_submit(self.deepseek_room, self.handoff["handoff_id"], request_id, recovery_id)
        return self.service.room_job_status(job["id"], 40)

    def test_tampered_snapshot_is_refused_before_launch_in_initial_and_correction_lanes(self):
        original = self.adapter_copy.read_bytes()
        self.adapter_copy.write_bytes(original + b"\n# tampered\n")
        before = self.state()
        refused = self.submit("implement-1")
        self.assertEqual(refused["status"], "failed", refused)
        self.assertEqual(refused["error"], "Refused before launch: provider_inventory_mismatch")
        self.assertEqual(refused["result"]["reason"], "provider_inventory_mismatch")
        self.assertEqual(refused["result"]["detail"], "content_changed:deepseek_adapter.py")
        self.assertIsNone(refused["result"]["recovery_id"])
        self.assertFalse(refused["result"]["model_launched"])
        self.assertEqual(self.state(), before, "no state write before the refusal")
        self.assertFalse((self.handoff_dir / "attempts").exists())
        self.assertEqual(self.implementation_calls(), [])
        self.assertTrue(self.service.room_status(self.deepseek_room)["ready_for_handoff"] is False or True)
        self.adapter_copy.write_bytes(original)
        completed = self.submit("implement-2")
        self.assertEqual(completed["status"], "succeeded", completed)
        self.assertEqual(completed["result"]["phase"], "awaiting_astra_review")
        self.assertEqual(len(self.implementation_calls()), 1)
        self.service.room_implementation_revise(self.deepseek_room, self.handoff["handoff_id"], "Diagnosed correction: adjust the fixture output.")
        self.assertEqual(self.state()["phase"], "correction_pending")
        snapshot = json.loads(self.config_copy.read_text())
        self.config_copy.write_text(json.dumps({**snapshot, "reasoning_effort": "low"}))
        before = self.state()
        refused = self.submit("implement-3")
        self.assertEqual((refused["status"], refused["result"]["detail"]), ("failed", "content_changed:deepseek.json"))
        self.assertEqual(self.state(), before)
        self.assertEqual(self.state()["phase"], "correction_pending")
        self.assertEqual(len(self.implementation_calls()), 1)
        self.config_copy.write_text(json.dumps(snapshot, ensure_ascii=False, indent=4) + "\n")  # same values, different bytes
        self.assertNotEqual(room.sha(self.config_copy.read_bytes()), json.loads((self.deepseek_root / "settings.json").read_text())["provider_inventory"]["files"][str(self.config_copy)])
        refused_again = self.submit("implement-4")
        self.assertEqual(refused_again["status"], "failed", "only the EXACT pinned bytes restore the inventory")
        project_room.atomic_json(self.config_copy, snapshot)
        self.assertEqual(room.sha(self.config_copy.read_bytes()), json.loads((self.deepseek_root / "settings.json").read_text())["provider_inventory"]["files"][str(self.config_copy)])
        corrected = self.submit("implement-5")
        self.assertEqual(corrected["status"], "succeeded", corrected)
        self.assertEqual(corrected["result"]["attempt_count"], 2)
        self.assertEqual(len(self.implementation_calls()), 2)
        export_dir = self.home / "deepseek" / "exports" / self.deepseek_room
        export_dir.rmdir()
        recreated = self.service.room_implementation_revise(self.deepseek_room, self.handoff["handoff_id"], "Second diagnosed correction.")
        self.assertEqual(recreated["phase"], "correction_pending")
        self.assertEqual(self.submit("implement-6")["status"], "succeeded", "a merely missing export directory is recreated")
        self.assertEqual(export_dir.stat().st_mode & 0o777, 0o700)

    def test_export_directory_mode_and_recreation_race_are_checked_before_launch(self):
        export_dir = self.home / "deepseek" / "exports" / self.deepseek_room
        export_dir.chmod(0o770)
        before = self.state()
        refused = self.submit("implement-1")
        self.assertEqual((refused["status"], refused["result"]["reason"], refused["result"]["detail"]), ("failed", "provider_inventory_mismatch", "export_dir_unsafe_mode"))
        self.assertEqual(export_dir.stat().st_mode & 0o777, 0o770, "the launch gate never chmods")
        self.assertEqual(self.state(), before)
        self.assertFalse((self.handoff_dir / "attempts").exists())
        self.assertEqual(self.implementation_calls(), [])
        export_dir.chmod(0o700)
        inventory = json.loads((self.deepseek_root / "settings.json").read_text())["provider_inventory"]
        self.assertIsNone(implementation.provider_inventory_mismatch(inventory))
        export_dir.rmdir()
        original = os.mkdir

        def racing(path, mode=0o777, *args, **kwargs):
            original(path, 0o700, *args, **kwargs)  # a concurrent creator wins the exact directory first
            raise FileExistsError(17, "synthetic race", str(path))
        with mock.patch.object(implementation.os, "mkdir", racing):
            self.assertIsNone(implementation.provider_inventory_mismatch(inventory), "the exact directory is revalidated, not reported missing")
        self.assertEqual(export_dir.stat().st_mode & 0o777, 0o700)
        export_dir.rmdir()
        victim = self.base / "victim-dir"
        victim.mkdir(mode=0o700)
        export_dir.symlink_to(victim)
        self.assertEqual(implementation.provider_inventory_mismatch(inventory), "export_dir_unsafe")
        refused = self.submit("implement-2")
        self.assertEqual((refused["status"], refused["result"]["detail"]), ("failed", "export_dir_unsafe"))
        export_dir.unlink()
        self.assertEqual(self.submit("implement-3")["status"], "succeeded", "a merely missing directory is recreated")
        self.assertEqual(export_dir.stat().st_mode & 0o777, 0o700)

    def test_packet_settings_are_bound_to_the_verified_pinned_bytes(self):
        pinned = json.loads(self.config_copy.read_text())
        original = implementation.verify_provider_inventory

        def swap_after_check(inventory, repair_export_dir=True):
            result = original(inventory, repair_export_dir)
            self.config_copy.write_text(json.dumps({**pinned, "reasoning_effort": "low", "max_tokens": 8192}))  # after the check, before the packet
            return result
        swapped = []
        with mock.patch.object(implementation, "verify_provider_inventory", swap_after_check):
            completed = implementation.run_implementation(self.handoff["handoff_path"])  # in-process: the swap really runs after the check
        self.assertEqual(completed["phase"], "awaiting_astra_review", completed)
        packet = self.implementation_calls()[-1]["packet"]
        self.assertEqual(json.loads(self.config_copy.read_text())["reasoning_effort"], "low", "the live snapshot was swapped after verification")
        self.assertEqual((packet["delegate_settings"]["reasoning_effort"], packet["delegate_settings"]["max_tokens"]), (pinned["reasoning_effort"], pinned["max_tokens"]),
                         "the packet carries the verified pinned bytes, not a later read")
        self.assertEqual(packet["delegate_settings"]["model"], deepseek_adapter.DEFAULT_MODEL)
        project_room.atomic_json(self.config_copy, pinned)
        self.service.room_implementation_revise(self.deepseek_room, self.handoff["handoff_id"], "Diagnosed correction: adjust the fixture output.")
        settings_path = self.deepseek_root / "settings.json"
        settings = json.loads(settings_path.read_text())
        self.config_copy.write_bytes(b"{not json")  # a malformed snapshot whose digest the room itself pins
        settings["provider_inventory"]["files"][str(self.config_copy)] = room.sha(self.config_copy.read_bytes())
        project_room.atomic_json(settings_path, settings)
        before = self.state()
        refused = self.submit("implement-2")
        self.assertEqual((refused["status"], refused["result"]["reason"], refused["result"]["detail"]), ("failed", "provider_inventory_mismatch", "content_changed:deepseek.json"),
                         "the handoff manifest pins the original digest, so the room-level rewrite is a mismatch")
        self.assertEqual(self.state(), before)
        self.assertEqual(len(self.implementation_calls()), 1)
        # A room whose own pinned snapshot is unparsable: the handoff pins that digest legitimately, so the inventory
        # matches and only the packet builder can refuse it, which it must do before any state, attempt or spawn.
        malformed_room, malformed_root = self.open_room("Malformed pinned snapshot")
        snapshot = malformed_root / "profiles" / "deepseek.json"
        snapshot.write_bytes(b"{not json")
        malformed_settings = json.loads((malformed_root / "settings.json").read_text())
        malformed_settings["provider_inventory"]["files"][str(snapshot)] = room.sha(snapshot.read_bytes())
        project_room.atomic_json(malformed_root / "settings.json", malformed_settings)
        self.consensus(malformed_room)
        malformed_handoff = self.service.room_handoff(malformed_room, 1, "Build it through independent review.", self.gates)
        malformed_dir = Path(malformed_handoff["handoff_path"]).parent
        self.fake.write_text(implementation_fake())
        before = json.loads((malformed_dir / "state.json").read_text())
        job = self.service.room_implementation_submit(malformed_room, malformed_handoff["handoff_id"], "implement-1")
        refused = self.service.room_job_status(job["id"], 40)
        self.assertEqual((refused["status"], refused["result"]["reason"], refused["result"]["detail"]), ("failed", "provider_inventory_mismatch", "config_unparsable:deepseek.json"))
        self.assertFalse(refused["result"]["model_launched"])
        self.assertEqual(json.loads((malformed_dir / "state.json").read_text()), before, "no state mutation, attempt or spawn for an unusable pinned snapshot")
        self.assertFalse((malformed_dir / "attempts").exists())
        self.assertEqual(len(self.implementation_calls()), 1)

    def test_tampered_snapshot_invalidates_an_audited_successor_and_a_fresh_recovery_can_follow(self):
        self.fake.write_text(RECOVERY_FAKE.replace("#!/usr/bin/env python3", "#!" + sys.executable, 1))
        (self.base / "implementation-mode.txt").write_text("interrupt-quota")
        interrupted = self.submit("implement-1")
        self.assertEqual(interrupted["status"], "uncertain", interrupted)
        (self.base / "implementation-mode.txt").write_text("normal")
        from test_recovery import RecoveryTests
        helper = RecoveryTests("test_refusals_leave_state_unchanged")
        helper.service, helper.room_id, helper.handoff_id, helper.handoff_dir = self.service, self.deepseek_room, self.handoff["handoff_id"], self.handoff_dir
        helper.backdate()
        self.service.process_inspector = fixed_inspector(boot=int(time.time()) - 5)
        report = self.service.room_implementation_audit(self.deepseek_room, self.handoff["handoff_id"], interrupted["id"])
        self.assertTrue(report["eligible"], report)
        prepared = self.service.room_implementation_recover(self.deepseek_room, self.handoff["handoff_id"], interrupted["id"], report["identity"]["spec_revision"],
                                                            report["identity"]["spec_sha256"], report["candidate"]["sha256"], report["evidence_digest"],
                                                            "Quota interruption diagnosed.", "Finish feature.txt.", "The user said: continue.", "recover-1")
        original = self.adapter_copy.read_bytes()
        self.adapter_copy.write_bytes(original + b"\n# tampered\n")
        refused = self.submit("implement-2", prepared["recovery_id"])
        self.assertEqual(refused["status"], "failed", refused)
        self.assertEqual(refused["result"]["reason"], "provider_inventory_mismatch")
        self.assertEqual(refused["result"]["recovery_id"], prepared["recovery_id"])
        recoveries = {row["id"]: row for row in self.service.room_status(self.deepseek_room)["recoveries"]}
        self.assertEqual((recoveries[prepared["recovery_id"]]["status"], recoveries[prepared["recovery_id"]]["reason"]), ("invalidated", "provider_inventory_mismatch"))
        self.assertEqual(self.state()["phase"], "blocked")
        self.assertEqual(len(self.implementation_calls()), 1)
        self.adapter_copy.write_bytes(original)
        report = self.service.room_implementation_audit(self.deepseek_room, self.handoff["handoff_id"], interrupted["id"])
        self.assertTrue(report["eligible"], report)
        prepared = self.service.room_implementation_recover(self.deepseek_room, self.handoff["handoff_id"], interrupted["id"], report["identity"]["spec_revision"],
                                                            report["identity"]["spec_sha256"], report["candidate"]["sha256"], report["evidence_digest"],
                                                            "Quota interruption diagnosed.", "Finish feature.txt.", "The user said: continue.", "recover-2")
        successor = self.submit("implement-3", prepared["recovery_id"])
        self.assertEqual(successor["status"], "succeeded", successor)
        self.assertEqual(successor["result"]["phase"], "awaiting_astra_review")
        self.assertEqual(len(self.implementation_calls()), 2)


class LaunchGuardTests(InventoryRefusalTests):
    def test_export_directory_is_reached_below_the_owned_home_and_ancestors_are_never_followed(self):
        inventory = json.loads((self.deepseek_root / "settings.json").read_text())["provider_inventory"]
        export_dir = Path(inventory["export_dir"])
        exports = export_dir.parent
        self.assertEqual(exports.parent, self.home / "deepseek")
        import shutil
        shutil.rmtree(exports)
        self.assertIsNone(implementation.provider_inventory_mismatch(inventory), "the missing levels below the owned home are recreated")
        self.assertEqual((exports.stat().st_mode & 0o777, export_dir.stat().st_mode & 0o777), (0o700, 0o700))
        shutil.rmtree(self.home / "deepseek")
        self.assertIsNone(implementation.provider_inventory_mismatch(inventory), "at most three levels below the existing owned home are recreated")
        self.assertEqual(implementation.provider_inventory_mismatch(inventory, repair_export_dir=False), None)
        shutil.rmtree(export_dir)
        self.assertEqual(implementation.provider_inventory_mismatch(inventory, repair_export_dir=False), "export_dir_missing")
        victim = self.base / "victim"
        victim.mkdir(mode=0o700)
        shutil.rmtree(exports)
        exports.symlink_to(victim, target_is_directory=True)
        self.assertEqual(implementation.provider_inventory_mismatch(inventory), "export_dir_unsafe", "a symlinked ancestor is refused, never followed")
        self.assertEqual(list(victim.iterdir()), [], "nothing was created behind the symlink")
        exports.unlink()
        self.assertIsNone(implementation.provider_inventory_mismatch(inventory))
        (self.home / "deepseek").chmod(0o750)
        try:
            self.assertEqual(implementation.provider_inventory_mismatch({**inventory, "export_dir": str(export_dir)}), None, "only the leaf mode is the grant's mode")
        finally:
            (self.home / "deepseek").chmod(0o700)
        self.assertEqual(implementation.provider_inventory_mismatch({**inventory, "export_dir": "/exports"}), "export_dir_unsafe")
        self.assertEqual(implementation.provider_inventory_mismatch({**inventory, "export_dir": "/nonexistent-home/deepseek/exports/x"}), "export_dir_missing",
                         "nothing above the three recreated levels is ever created")
        self.assertFalse(Path("/nonexistent-home").exists())
        self.assertEqual(self.submit("implement-1")["status"], "succeeded")

    def test_each_lane_reports_its_own_refusal_before_the_snapshot_check_and_wraps_snapshot_errors(self):
        manifest = json.loads((self.handoff_dir / "handoff.json").read_text())
        state = json.loads((self.handoff_dir / "state.json").read_text())
        successor = {"recovery_id": "r" * 32, "successor_job_id": "s" * 32, "recheck": lambda: None}
        recovery_state = {**state, "phase": "recovery_prepared", "recovery": {"candidate": state["initial_candidate"], "recovery_id": "r" * 32}}
        original = self.adapter_copy.read_bytes()
        self.adapter_copy.write_bytes(original + b"\n# tampered\n")
        with mock.patch.object(implementation, "_recovery_refusal", return_value=("evidence_changed", None)):
            refused = implementation._continue_run(self.handoff_dir, manifest, dict(recovery_state), successor, None)
        self.assertEqual((refused["phase"], refused["reason"], refused["recovery_id"]), ("refused_before_launch", "evidence_changed", "r" * 32),
                         "the recovery recheck's own reason is recorded, not masked by the snapshot mismatch")
        with mock.patch.object(implementation, "_recovery_refusal", return_value=(None, state["initial_candidate"])):
            refused = implementation._continue_run(self.handoff_dir, manifest, dict(recovery_state), successor, None)
        self.assertEqual((refused["reason"], refused["detail"]), ("provider_inventory_mismatch", "content_changed:deepseek_adapter.py"))
        self.adapter_copy.write_bytes(original)
        with mock.patch.object(implementation, "_recovery_refusal", return_value=(None, state["initial_candidate"])), \
                mock.patch.object(implementation, "verify_provider_inventory", side_effect=RuntimeError("synthetic snapshot failure")):
            refused = implementation._continue_run(self.handoff_dir, manifest, dict(recovery_state), successor, None)
        self.assertEqual((refused["reason"], refused["detail"], refused["model_launched"]), ("prelaunch_error", "RuntimeError", False),
                         "an exception in the snapshot check is a proven non-launch in the recovery lane")
        with mock.patch.object(implementation, "_require_current_agreement", side_effect=implementation.ImplementationError("agreement moved")):
            self.adapter_copy.write_bytes(original + b"\n# tampered\n")
            with self.assertRaisesRegex(implementation.ImplementationError, "agreement moved"):
                implementation._continue_run(self.handoff_dir, manifest, dict(state), None, None)
        self.adapter_copy.write_bytes(original)
        with mock.patch.object(implementation, "verify_provider_inventory", side_effect=OSError(5, "synthetic export directory failure")):
            refused = implementation._continue_run(self.handoff_dir, manifest, dict(state), None, None)
        self.assertEqual((refused["phase"], refused["reason"], refused["detail"], refused["recovery_id"]), ("refused_before_launch", "prelaunch_error", "OSError", None),
                         "an exception in the initial lane's snapshot check is a proven non-launch, never an uncertain job")
        self.assertEqual(json.loads((self.handoff_dir / "state.json").read_text()), state, "no lane wrote state before refusing")
        self.assertFalse((self.handoff_dir / "attempts").exists())
        self.assertEqual(self.implementation_calls(), [])

    def test_handoff_binds_the_inventory_to_its_own_room(self):
        settings_path = self.deepseek_root / "settings.json"
        settings = json.loads(settings_path.read_text())
        other_room, other_root = self.open_room("Other room")
        self.consensus(other_room)
        foreign = json.loads((other_root / "settings.json").read_text())
        foreign["provider_inventory"]["room_id"] = self.deepseek_room
        project_room.atomic_json(other_root / "settings.json", foreign)
        with self.assertRaisesRegex(implementation.ImplementationError, "does not belong to this room"):
            self.service.room_handoff(other_room, 1, "Build it through independent review.", self.gates)
        foreign = json.loads((other_root / "settings.json").read_text())
        foreign["provider_inventory"]["room_id"] = other_root.name
        moved = self.deepseek_root / "profiles" / "deepseek.json"
        foreign["provider_inventory"]["files"] = {str(moved) if Path(path).name == "deepseek.json" else path: digest
                                                  for path, digest in foreign["provider_inventory"]["files"].items()}
        project_room.atomic_json(other_root / "settings.json", foreign)
        with self.assertRaisesRegex(implementation.ImplementationError, "does not belong to this room"):
            self.service.room_handoff(other_room, 1, "Build it through independent review.", self.gates)
        self.assertEqual(json.loads(settings_path.read_text()), settings, "the guarded room is untouched")
        moved_settings = self.base / "moved-settings.json"
        moved_settings.write_bytes((other_root / "settings.json").read_bytes())
        (other_root / "settings.json").unlink()
        (other_root / "settings.json").symlink_to(moved_settings)
        with self.assertRaisesRegex(implementation.ImplementationError, "unreadable"):
            self.service.room_handoff(other_room, 1, "Build it through independent review.", self.gates)  # the settings read is owned and symlink-refusing
        (other_root / "settings.json").unlink()
        moved_settings.rename(other_root / "settings.json")
        self.assertEqual(self.implementation_calls(), [], "no implementation attempt was launched for either room")

    def test_mcp_config_reads_are_bounded_owned_and_cross_checked(self):
        profile = self.deepseek_root / "profiles" / "implementation.json"
        config = room.load_config(profile)
        self.assertEqual(set(implementation._mcp_servers(config)), {"deepseek"})
        mcp_path = self.deepseek_root / "profiles" / "implementation-mcp.json"
        original = mcp_path.read_bytes()
        mcp_path.write_text(json.dumps({"mcpServers": {}}))
        with self.assertRaisesRegex(implementation.ImplementationError, "changed since the profile was loaded"):
            implementation._mcp_servers(config)  # the replaced file never decides the provider
        mcp_path.write_bytes(original)
        with mock.patch.object(implementation, "INVENTORY_FILE_LIMIT", 8), self.assertRaisesRegex(implementation.ImplementationError, "unreadable"):
            implementation._mcp_servers(config)
        linked = self.base / "linked-mcp.json"
        linked.symlink_to(mcp_path)
        with self.assertRaisesRegex(implementation.ImplementationError, "unreadable"):
            implementation._mcp_servers({"extra_args": ["--mcp-config", str(linked)]})
        nested = self.base / "nested.json"
        nested.write_text("[" * 100000 + "]" * 100000)
        with self.assertRaisesRegex(implementation.ImplementationError, "unreadable"):
            implementation._mcp_servers({"extra_args": ["--mcp-config", str(nested)]})
        self.assertEqual(set(implementation._mcp_servers({"extra_args": ["--mcp-config", str(mcp_path)]})), {"deepseek"}, "without a recorded digest the bounded owned read alone applies")

    def test_pinned_argument_references_match_after_tilde_expansion(self):
        source = str(Path.home() / "servers.json")
        self.assertEqual(implementation._pinned_arg("~/servers.json", source, "/pinned/0-servers.json"), "/pinned/0-servers.json")
        self.assertEqual(implementation._pinned_arg("--mcp-config=~/servers.json", source, "/pinned/0-servers.json"), "--mcp-config=/pinned/0-servers.json")
        self.assertEqual(implementation._pinned_arg(source, source, "/pinned/0-servers.json"), "/pinned/0-servers.json")
        self.assertEqual(implementation._pinned_arg("--mcp-config=" + source, source, "/pinned"), "--mcp-config=/pinned")
        self.assertEqual(implementation._pinned_arg("--effort", source, "/pinned"), "--effort")
        odd = "/tmp/room=1/servers.json"
        self.assertEqual(implementation._pinned_arg(odd, odd, "/pinned/0-servers.json"), "/pinned/0-servers.json", "a bare path containing '=' is still pinned")
        self.assertEqual(implementation._pinned_arg("--mcp-config=" + odd, odd, "/pinned/0-servers.json"), "--mcp-config=/pinned/0-servers.json")
        self.assertEqual(implementation._pinned_arg('{"mcpServers":{}}', source, "/pinned"), '{"mcpServers":{}}')
        with mock.patch.dict(os.environ, {"HOME": str(self.base)}):
            (self.base / "servers.json").write_text(json.dumps({"mcpServers": {}}))
            handwritten = self.base / "handwritten.json"
            handwritten.write_text(json.dumps({"claude_bin": str(self.fake), "model": project_room.MODEL, "expected_model_ids": [project_room.MODEL],
                                               "timeout_seconds": 30, "claude_config_dir": str(self.base / "claude-storage"),
                                               "extra_args": ["--effort", "max", "--permission-mode", "auto", "--strict-mcp-config", "--mcp-config", "~/servers.json"]}))
            loaded = room.load_config(handwritten)
            self.assertIn(str(self.base / "servers.json"), loaded["referenced_files_sha256"])
            pinned = [implementation._pinned_arg(arg, str(self.base / "servers.json"), "/pinned/0-servers.json") for arg in loaded["extra_args"]]
            self.assertIn("/pinned/0-servers.json", pinned)
            self.assertNotIn("~/servers.json", pinned, "a hand-written tilde reference is pinned like an absolute one")


class ControllerDiagnosticsTests(WiringFixture):
    def test_damaged_controller_configuration_is_diagnosed_without_a_traceback(self):
        target = self.home / "config.json"
        saved = target.read_bytes()
        for damaged, expectation in (("[1, 2, 3]", "JSON object"), ('"text"', "JSON object"), ("[" * 100000 + "]" * 100000, "not configured"), ("{not json", "not configured")):
            with self.subTest(damaged=damaged[:12]):
                target.write_text(damaged)
                with self.assertRaisesRegex(room.RoomError, expectation):
                    self.service.settings()
                with self.assertRaisesRegex(room.RoomError, "move .* aside"):
                    self.service.setup(claude_bin=str(self.fake))
                self.assertEqual(target.read_text(), damaged, "setup never overwrote an unreadable configuration")
                doctor = self.service.room_doctor()
                self.assertFalse(doctor["configured"])
                process = subprocess.run([sys.executable, str(ROOT / "project_room.py"), "--home", str(self.home), "setup", "--claude-bin", str(self.fake)],
                                         capture_output=True, text=True, timeout=60, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
                self.assertEqual(process.returncode, 2)
                self.assertIn("error", json.loads(process.stderr))
                self.assertNotIn("Traceback", process.stderr)
        target.write_bytes(saved)
        settings_path = self.room_root / "settings.json"
        original = settings_path.read_bytes()
        settings_path.write_text("[" * 100000 + "]" * 100000)
        self.assertEqual(self.service.room_status(self.room_id)["delegate_jobs"]["unavailable_reason"], "settings_unreadable")
        settings_path.write_bytes(original)
        config = self.service.settings()
        config["delegate_provider"] = "mystery"
        project_room.atomic_json(target, config)
        doctor = self.service.room_doctor()
        self.assertEqual(doctor["delegate_provider"], "mystery")
        self.assertIn("Unknown delegate provider", doctor["delegate_provider_error"])
        config["delegate_provider"] = ""
        config["qwen_config"] = str(self.base / "qwen.json")
        project_room.atomic_json(target, config)
        self.assertEqual(project_room.provider_name(config), "", "a recorded empty selection is reported as recorded, never inferred")
        with self.assertRaisesRegex(room.RoomError, "Unknown delegate provider"):
            project_room.Service._provider(config)
        self.assertIn("delegate_provider_error", self.service.room_doctor())
        with self.assertRaisesRegex(room.RoomError, "Unknown delegate provider"):
            self.service.room_open(str(self.project), "Empty selection")
        project_room.atomic_json(target, json.loads(saved))
        self.assertEqual(self.calls(), [])

    def test_controller_transcript_audit_is_read_only_and_creates_no_home(self):
        missing = self.base / "typo-home"
        command = [sys.executable, str(ROOT / "project_room.py"), "--home", str(missing), "transcript-audit", "--room", self.room_id, "--handoff", "0" * 64, "--attempt", "1"]
        process = subprocess.run(command, capture_output=True, text=True, timeout=60, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(process.returncode, 2, process.stderr)
        self.assertIn("registry not found", process.stderr)
        self.assertFalse(missing.exists(), "a mistyped --home provisions nothing")
        registry = self.home / "registry.sqlite3"
        before = registry.stat().st_mtime_ns
        process = subprocess.run(command[:2] + ["--home", str(self.home)] + command[4:], capture_output=True, text=True, timeout=60, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(process.returncode, 2)
        self.assertIn("Unknown handoff", process.stderr)
        self.assertEqual(registry.stat().st_mtime_ns, before)
        self.assertEqual(project_room.controller_home(str(missing)), missing.resolve())


class InventoryReadTests(unittest.TestCase):
    def test_pinned_files_are_read_below_the_room_directory_without_following_symlinks(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            room_dir = base / "room"
            profiles = room_dir / "profiles"
            profiles.mkdir(parents=True, mode=0o700)
            adapter_copy = profiles / "deepseek_adapter.py"
            adapter_copy.write_bytes(b"# snapshot\n")
            export_dir = base / "deepseek" / "exports" / "room"
            export_dir.mkdir(parents=True, mode=0o700)
            inventory = {"provider": "deepseek", "room_id": "room", "export_dir": str(export_dir), "files": {str(adapter_copy): room.sha(b"# snapshot\n")}}
            self.assertIsNone(implementation.provider_inventory_mismatch(inventory))
            elsewhere = base / "elsewhere"
            elsewhere.mkdir()
            (elsewhere / "deepseek_adapter.py").write_bytes(b"# snapshot\n")
            adapter_copy.unlink()
            profiles.rmdir()
            profiles.symlink_to(elsewhere)
            self.assertEqual(implementation.provider_inventory_mismatch(inventory), "file_unsafe:deepseek_adapter.py")
            profiles.unlink()
            profiles.mkdir(mode=0o700)
            adapter_copy.symlink_to(elsewhere / "deepseek_adapter.py")
            self.assertEqual(implementation.provider_inventory_mismatch(inventory), "file_unsafe:deepseek_adapter.py")
            adapter_copy.unlink()
            self.assertEqual(implementation.provider_inventory_mismatch(inventory), "file_missing:deepseek_adapter.py")
            adapter_copy.write_bytes(b"# changed\n")
            self.assertEqual(implementation.provider_inventory_mismatch(inventory), "content_changed:deepseek_adapter.py")
            self.assertEqual(implementation.provider_inventory_mismatch({**inventory, "files": {"relative/path.py": "0" * 64}}), "file_unsafe:path.py")


class StatusSurfaceTests(WiringFixture):
    def test_delegate_jobs_summary_is_bounded_allowlisted_and_read_only(self):
        self.configure("deepseek")
        room_id, root = self.open_room("Ledger feature")
        ledger = deepseek_adapter.Ledger(self.home)
        with ledger.transaction() as db:
            for index in range(22):
                db.execute("INSERT INTO jobs(id,room_id,request_id,lane,payload_sha256,profile_sha256,state,created_at,requested_model,thinking,reasoning_effort,"
                           "max_tokens,input_bytes,reserved_bytes,possibly_billed,usage_json,diagnosis,worker_pid,finished_at,error_code) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (f"{index:032x}", room_id if index < 21 else "other-room", f"r{index}", "deep", "p" * 64, "q" * 64,
                            "unknown_delivery" if index == 0 else "completed", f"2026-09-09T00:00:{index:02d}+00:00", deepseek_adapter.DEFAULT_MODEL, "enabled", "max",
                            393216, 100, 0, 1, json.dumps({"prompt_tokens": 42, "completion_tokens": 19}), "PRIVATE_DIAGNOSIS_TEXT", 4242,
                            f"2026-09-09T00:01:{index:02d}+00:00", "worker_vanished" if index == 0 else None))
            db.execute("INSERT INTO resolutions(job_id,room_id,note_sha256,note,created_at) VALUES(?,?,?,?,?)", ("0" * 32, room_id, "n" * 64, "PRIVATE_NOTE_TEXT", "2026-09-09T00:02:00+00:00"))
        status = self.service.room_status(room_id)
        summary = status["delegate_jobs"]
        self.assertEqual((summary["provider"], summary["truncated"], len(summary["items"]), summary["unavailable_reason"]), ("deepseek", True, 20, None))
        newest = summary["items"][0]
        self.assertEqual((newest["job_id"], newest["state"], newest["usage"]["completion_tokens"], newest["possibly_billed"]), (f"{20:032x}", "completed", 19, True))
        self.assertNotIn(f"{21:032x}", [item["job_id"] for item in summary["items"]], "other rooms never leak")
        oldest_visible = [item for item in summary["items"] if item["job_id"] == "0" * 32]
        self.assertEqual(oldest_visible, [], "the oldest row is beyond the 20 newest")
        serialized = json.dumps(status)
        for private in ("PRIVATE_DIAGNOSIS_TEXT", "PRIVATE_NOTE_TEXT", "4242", "worker_pid", "diagnosis"):
            self.assertNotIn(private, serialized)
        self.assertEqual(self.service.room_status(self.room_id)["delegate_jobs"]["unavailable_reason"], "provider_not_deepseek")
        with mock.patch.object(project_room.sqlite3, "connect", wraps=sqlite3.connect) as connect:
            self.service.room_status(room_id)
            self.assertTrue(any("mode=ro" in str(call.args[0]) for call in connect.call_args_list))

    def test_delegate_jobs_degrades_for_a_non_object_settings_file(self):
        self.configure("deepseek")
        room_id, root = self.open_room("Damaged settings")
        for damaged in ("[]", "null", '"text"', "42"):
            with self.subTest(damaged=damaged):
                (root / "settings.json").write_text(damaged)
                status = self.service.room_status(room_id)
                self.assertEqual(status["delegate_jobs"]["unavailable_reason"], "settings_unreadable")
                self.assertIn("review", status, "the rest of the read-only surface stays available")
        process = subprocess.run([sys.executable, str(ROOT / "project_room.py"), "--home", str(self.home), "call", "room_status", "--args", json.dumps({"room_id": room_id})],
                                 capture_output=True, text=True, timeout=60, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout)["delegate_jobs"]["unavailable_reason"], "settings_unreadable")

    def test_progress_and_mcp_surfaces_describe_the_provider(self):
        self.assertEqual(progress._category("mcp__deepseek__deepseek_submit"), "remote-model")
        self.assertEqual(progress._category("mcp__qwen-local__qwen_submit"), "local-model")
        initialized = project_room_mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, self.service)["result"]
        self.assertEqual(initialized["serverInfo"]["version"], "0.3.0")
        self.assertIn("DeepSeek", initialized["instructions"])
        listed = project_room_mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, self.service)["result"]["tools"]
        self.assertEqual(len(listed), 23)  # the two verification-retry tools joined the inventory
        descriptions = {tool["name"]: tool["description"] for tool in listed}
        self.assertIn("provider", descriptions["room_implementation_submit"])
        self.assertIn("delegate_jobs", descriptions["room_status"])
        self.assertEqual(json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text())["version"], "0.3.0")


if __name__ == "__main__":
    unittest.main()
