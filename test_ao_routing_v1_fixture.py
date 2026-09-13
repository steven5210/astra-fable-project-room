"""Actual frozen-v1 compatibility and normalization transport contracts, without inference or Git-history reads."""

import contextlib
import copy
import io
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import ao_delegates
import ao_project_room as ao
import ao_routing
import ao_workflow
import project_room
import project_room_mcp
from test_ao_normal import Fixture
import test_ao_response_normalization as normalization_tests


FIXTURES = Path(__file__).parent / "testdata/ao-routing-v1"
BASELINE = "a797bf7a3f327c0bd9ab2001caca154ff48c67ef"
V1_GUARD_SHA256 = "49078d6b90bfa2602fd007a8eb28362c8afcf6bf13f55ab324191b6d9593dab3"


class FrozenV1RoutingTests(Fixture):
    def setUp(self):
        super().setUp()
        self.fixture = json.loads((FIXTURES / "fixture.json").read_text())
        self.assertEqual(self.fixture["source_commit"], BASELINE)
        self.assertEqual(ao.digest((FIXTURES / "guard.py").read_bytes()), V1_GUARD_SHA256)
        self.assertEqual(self.fixture["guard_sha256"], V1_GUARD_SHA256)
        self.room = self.open(provider="none"); self.spec()
        # Build the historical preparation directly from frozen data. No v2 guard or settings are generated first.
        with patch.object(ao_routing, "prepare", self.install_frozen_routing):
            self.prepared = self.service.ao_room_prepare(self.room, str(self.repo))

    def install_frozen_routing(self, service, directory, state, worktree, prepared):
        guard = service.root / "launchers" / (V1_GUARD_SHA256 + ".py")
        guard.parent.mkdir(parents=True, exist_ok=True)
        guard.write_bytes((FIXTURES / "guard.py").read_bytes()); guard.chmod(0o600)
        command = ao_routing.hook_command(sys.executable, guard)
        settings = copy.deepcopy(self.fixture["settings_template"])
        settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"] = command
        documents = {".claude/settings.local.json": (json.dumps(settings, indent=2, sort_keys=True) + "\n").encode()}
        for name, text in self.fixture["agent_definitions"].items():
            documents[".claude/agents/" + name + ".md"] = text.encode()
        for relative, content in documents.items():
            target = worktree / relative; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(content)
        routing = copy.deepcopy(self.fixture["routing_template"])
        routing.update(files={name: ao.digest(data) for name, data in documents.items()}, guard_path=str(guard),
                       guard_sha256=V1_GUARD_SHA256, hook_command=command, python=sys.executable,
                       claude_config_dir=str(self.claude_env), claude=ao_routing.claude_evidence(None),
                       rules=ao_routing.observe_rules(self.fake, state), prepare_environment={})
        prepared["routing"] = routing
        return routing

    def frozen_bytes(self):
        files = [self.repo / p for p in self.prepared["routing"]["files"]]
        files += [Path(self.prepared["routing"]["guard_path"]), self.directory() / "preparation.json",
                  self.directory() / "state.json"]
        return {str(p): p.read_bytes() for p in files}

    def test_validate_and_reprepare_leave_actual_v1_guard_settings_and_preparation_unchanged(self):
        routing = self.prepared["routing"]
        self.assertEqual(routing["version"], 1)
        self.assertNotIn("execution_policy", routing)
        self.assertEqual(routing["matcher"], "Agent|Workflow|Task|Skill|SendMessage|Team.*|mcp__deepseek__.*|mcp__qwen-local__.*|mcp__project-room__.*")
        settings = json.loads((self.repo / ".claude/settings.local.json").read_text())
        self.assertEqual(settings["hooks"]["PreToolUse"][0]["matcher"], routing["matcher"])
        before = self.frozen_bytes()
        with patch.object(ao_routing, "prepare", side_effect=AssertionError("must not generate or relabel a v2 preparation")):
            self.assertEqual(ao_routing.validate_local(self.prepared), routing)
            self.assertEqual(self.service.ao_room_prepare(self.room, str(self.repo)), self.prepared)
            status = self.service.ao_room_status(self.room)["delegate"]["routing"]
        self.assertEqual(status["status"], "configured")
        self.assertEqual(status["execution_policy"], "historical_unrestricted_root")
        self.assertEqual(self.frozen_bytes(), before)
        self.assertEqual(Path(routing["guard_path"]).read_bytes(), (FIXTURES / "guard.py").read_bytes())
        # A historical root Bash call still takes no guard decision: the fixture is a real old guard, not v2 with a relabeled version.
        response = subprocess.run([sys.executable, routing["guard_path"]], input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "synthetic only; never executed"}}),
                                  capture_output=True, text=True, check=True)
        self.assertEqual(response.stdout, "")
        self.assertEqual(self.fake.posts, [])

    def test_every_workflow_part_and_delivered_hash_stays_exactly_v1(self):
        policy = ao_delegates.validate_provider(self.directory(), self.state())
        self.assertEqual(policy, self.fixture["policy"])
        actual = ao_workflow.part_texts(self.prepared, policy)
        self.assertEqual(actual, self.fixture["workflow_parts"])
        self.assertNotIn("Fable is the orchestrator: inspect evidence", actual["routing"])
        self.assertNotIn("The entire final response must be that JSON object", actual["review_contract"])
        self.assertIn("Fable owns implementation, engineering review and eligible delegation", actual["report_contract"])
        self.service.ao_room_bind(self.room, "engineer", "engineer", ao_workflow.FABLE_MODEL, "max")
        self.agree()
        request = copy.deepcopy(self.state()["requests"]["spec_review"])
        self.assertEqual({name: request["carried"]["part_sha256"][name] for name in actual},
                         {name: ao.digest(value.encode()) for name, value in actual.items()})
        from ao_report_contract import PART, INSTRUCTION
        self.assertEqual(request["carried"]["part_sha256"][PART], ao.digest(INSTRUCTION.encode()))
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.service.ao_room_send(self.room, "engineer", "Continue.", "continuation", purpose="implementation")
        self.assertEqual(self.fake.posts[-1][1]["text"], "Continue.")
        self.assertEqual(self.state()["requests"]["spec_review"], request)
        self.assertEqual(self.state()["requests"]["continuation"]["carried"]["parts"], [])


class NormalizationInterfaceTests(Fixture):
    spec_result = normalization_tests.ResponseNormalizationTests.spec_result
    finish_spec = normalization_tests.ResponseNormalizationTests.finish_spec
    args = normalization_tests.ResponseNormalizationTests.args

    def setUp(self):
        super().setUp()
        self.room = self.open(); self.spec(); self.bind()

    def test_mcp_annotation_marks_normalization_as_a_state_write(self):
        response = project_room_mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, project_room.Service(self.home))
        matches = [item for item in response["result"]["tools"] if item["name"] == "ao_room_response_normalize"]
        self.assertEqual(len(matches), 1)
        self.assertIs(matches[0]["annotations"]["readOnlyHint"], False)
        self.assertIs(matches[0]["annotations"]["destructiveHint"], False)
        self.assertEqual(set(matches[0]["inputSchema"]["required"]), set(self.args_schema_fields()))
        self.assertEqual(self.fake.posts, [])

    @staticmethod
    def args_schema_fields():
        return ("room_id", "request_id", "receipt_sha256", "final_text_sha256", "json_start", "json_end", "astra_review", "confirm_no_additional_verdict")

    def test_real_cli_args_file_preserves_multiline_review_and_dispatches_no_model(self):
        self.finish_spec()
        review = "I inspected the entire response.\nThe wrapper adds no verdict; literal quotes and $(text) are data."
        args = self.args(astra_review=review)
        path = self.root / "normalization arguments.json"; path.write_text(json.dumps(args))
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), patch.object(ao.Service, "client", lambda service, state: self.fake), patch.object(project_room.signal, "signal"):
            exit_code = project_room.main(["--home", str(self.home), "call", "ao_room_response_normalize", "--args-file", str(path)])
        self.assertEqual(exit_code, 0)
        self.assertTrue(json.loads(stdout.getvalue())["normalized"])
        request = self.state()["requests"]["spec_review"]
        record = ao.read(self.directory() / request["response_normalization"])
        self.assertEqual(record["inputs"]["astra_review"], review)
        self.assertEqual(len(self.fake.posts), 1)
