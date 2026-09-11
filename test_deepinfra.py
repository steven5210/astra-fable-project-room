"""Hosted DeepSeek transport contracts. Synthetic keys and injected loopback transport only."""

import contextlib
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import unittest
from unittest import mock

import deepseek_adapter as adapter
import implementation
import test_deepseek_adapter as fixtures
from test_deepseek_wiring import WiringFixture


class DeepInfraFixture(fixtures.AdapterFixture):
    config_overrides = {**fixtures.FAST, "backend": "deepinfra"}

    def setUp(self):
        super().setUp()
        target = self.secrets / "deepinfra-api-key"
        self.key_path.rename(target)
        self.key_path = target
        self.fake.scenario = {"kind": "ok", "model": adapter.DEEPINFRA_MODEL, "reasoning_key": "reasoning"}

    def run_case(self, name, scenario):
        instance = self.room(name)
        terminal = self.run_to_terminal(instance, "Task " + name, "request-1", {
            "kind": "ok", "model": adapter.DEEPINFRA_MODEL, "reasoning_key": "reasoning", **scenario})
        return instance, terminal


class DeepInfraTransportTests(DeepInfraFixture):
    def test_host_path_body_key_and_durable_results_are_backend_specific(self):
        job = self.submit()
        status = self.wait(job["job_id"])
        self.assertEqual(status["state"], "completed", status)
        request = self.fake.requests[0]
        self.assertEqual(request["headers"]["X-Fixture-Target"], "api.deepinfra.com:443")
        self.assertEqual(request["path"], "/v1/openai/chat/completions")
        self.assertEqual(request["headers"]["Authorization"], "Bearer " + fixtures.SYNTHETIC_KEY)
        body = request["body"]
        self.assertEqual(set(body), {"model", "messages", "stream", "max_tokens", "reasoning_effort", "stream_options"})
        self.assertEqual((body["model"], body["reasoning_effort"], body["max_tokens"]),
                         (adapter.DEEPINFRA_MODEL, "max", fixtures.FAST["max_tokens"]))
        self.assertEqual(body["stream_options"], {"include_usage": True})
        self.assertTrue(body["stream"])
        self.assertEqual((status["backend"], status["observed_model"]), ("deepinfra", adapter.DEEPINFRA_MODEL))
        result = self.adapter.result(job["job_id"])
        self.assertEqual(result["text"], "Hello, wörld OK")
        self.assertEqual(adapter.sha(Path(result["content_path"]).read_bytes()), result["content_sha256"])
        for name in ("request.json", "meta.json"):
            self.assertEqual(json.loads((self.job_dir(job["job_id"]) / name).read_text())["backend"], "deepinfra")
        self.assertIn("PRIVATE_REASONING_MUST_NOT_LEAK", (self.job_dir(job["job_id"]) / "reasoning").read_text())
        surfaced = json.dumps([status, result, self.adapter.health()])
        for secret in (fixtures.SYNTHETIC_KEY, "PRIVATE_REASONING_MUST_NOT_LEAK"):
            self.assertNotIn(secret, surfaced)
        self.assertEqual(self.submit()["job_id"], job["job_id"])
        self.assertEqual(len(self.fake.requests), 1, "idempotent reuse sends nothing")

    def test_ask_none_and_low_preserve_the_small_lane_without_a_thinking_object(self):
        for effort in ("none", "low"):
            result = self.adapter.ask("Quick " + effort, "ask-" + effort, effort=effort)
            self.assertEqual(result["state"], "completed", result)
            body = self.fake.requests[-1]["body"]
            self.assertEqual((body["reasoning_effort"], body["max_tokens"]), (effort, fixtures.FAST["ask_max_tokens"]))
            self.assertNotIn("thinking", body)
        with self.assertRaises(adapter.AdapterError) as rejected:
            self.adapter.ask("Too high", "ask-high", effort="high")
        self.assertEqual(rejected.exception.code, "effort_invalid")
        self.assertEqual(len(self.fake.requests), 2)

    def test_reasoning_variants_usage_only_chunks_and_unknown_usage(self):
        for key, placement in (("reasoning", "separate"), ("reasoning_content", "final")):
            _, status = self.run_case(key, {"reasoning_key": key, "usage": placement})
            self.assertEqual(status["state"], "completed", status)
            self.assertEqual(status["usage"], fixtures.USAGE)
            self.assertEqual(status["usage_source"], "usage_only_chunk" if placement == "separate" else "final_chunk")
            self.assertGreater(status["reasoning_bytes"], 0)
        _, missing = self.run_case("missing-usage", {"usage": "missing"})
        self.assertEqual((missing["state"], missing["usage"], missing["usage_source"]), ("completed", None, "missing"))
        _, unknown = self.run_case("unknown-usage", {"usage": "final", "usage_value": {"unknown": 12}})
        self.assertEqual((unknown["state"], unknown["usage"], unknown["usage_source"]), ("completed", None, "unrecognized"))

    def test_model_identity_and_stream_failures_block_without_retry_or_fallback(self):
        cases = [({"model": None}, "model_unverified"),
                 ({"model": "deepseek-ai/deepseek-v4.1-flash"}, "model_mismatch"),
                 ({"model": adapter.DEFAULT_MODEL}, "model_mismatch"),
                 ({"premature": True}, "stream_incomplete"),
                 ({"reasoning_value": {"unexpected": "object"}}, "malformed_stream"),
                 ({"kind": "redirect"}, "redirect_refused")]
        for index, (scenario, code) in enumerate(cases):
            with self.subTest(code=code):
                # Reset between scenarios; each failed room remains stopped.
                self.fake.scenario = {"kind": "ok", "model": adapter.DEEPINFRA_MODEL, "reasoning_key": "reasoning"}
                instance, status = self.run_case(f"bad-stream-{index}", scenario)
                self.assertEqual((status["state"], status["error_code"]), ("failed_after_send", code), status)
                self.assertTrue(status["stops_room_lane"])
                self.assertTrue(status["possibly_billed"])
                with self.assertRaises(adapter.AdapterError) as blocked:
                    instance.submit("Different task", "request-2", diagnosis="No automatic retry is permitted")
                self.assertEqual(blocked.exception.code, "room_stopped")
                self.assertEqual(len(self.fake.requests), index + 1)
        self.assertTrue(all(r["headers"]["X-Fixture-Target"] == "api.deepinfra.com:443" for r in self.fake.requests))

    def test_definite_rejection_and_ambiguous_delivery_keep_distinct_states(self):
        cases = [(401, None, "authentication_error", "rejected_before_generation", False),
                 (429, None, "engine_overloaded", "rejected_before_generation", False),
                 (429, None, "rate_limited", "rejected_before_generation", False),
                 (401, b"<html>upstream denied</html>", "authentication_error", "failed_after_send", True),
                 (503, None, "engine_overloaded", "failed_after_send", True)]
        for index, (code, body, error_type, state, billed) in enumerate(cases):
            scenario = {"kind": "http_error", "status": code, "type": error_type}
            if body is not None:
                scenario.update(body=body, content_type="text/html")
            with self.subTest(status=code, error_type=error_type, body=body):
                _, status = self.run_case(f"http-{index}", scenario)
                self.assertEqual((status["state"], status["possibly_billed"], status["stops_room_lane"]), (state, billed, billed))
                if body is None:
                    self.assertEqual(status["error_type"], error_type)
                self.assertNotIn("PROVIDER_PROSE_MUST_NOT_LEAK", json.dumps(status))
        self.assertEqual(len(self.fake.requests), len(cases))

    def test_missing_hosted_key_never_uses_the_official_key_or_environment(self):
        self.key_path.unlink()
        official = self.secrets / "deepseek-api-key"
        official.write_text("sk-synthetic-official-only\n")
        official.chmod(0o600)
        with mock.patch.dict("os.environ", {"DEEPINFRA_API_KEY": "sk-synthetic-environment"}):
            status = self.wait(self.submit()["job_id"])
        self.assertEqual(status["state"], "not_started", status)
        self.assertFalse(status["possibly_billed"])
        self.assertEqual(self.fake.requests, [])

    def test_probe_connect_failure_is_unsent_and_does_not_retry(self):
        with mock.patch.object(adapter, "_open_connection", side_effect=socket.gaierror("synthetic DNS failure")) as connection:
            receipt = adapter.run_probe(self.adapter)
        self.assertEqual(connection.call_count, 1)
        self.assertEqual(connection.call_args.args[:2], ("api.deepinfra.com", 443))
        self.assertEqual((receipt["state"], receipt["error_code"]), ("not_started", "transport_connect_failed"))
        self.assertEqual(receipt["backend"], "deepinfra")
        self.assert_unsuccessful_probe_is_candid(receipt, self.adapter)
        self.assertEqual(self.fake.requests, [])

    def assert_unsuccessful_probe_is_candid(self, receipt, instance):
        self.assertNotEqual(receipt["state"], "completed")
        self.assertIsNone(receipt["observed_model"])
        self.assertIsNone(receipt["expected_answer_matched"])
        self.assertNotIn("endpoint accepted the credential", receipt["meaning"])
        self.assertIn("does not establish", receipt["meaning"])
        with mock.patch.object(adapter, "read_api_key", side_effect=AssertionError("health must not read a key")):
            latest = instance.health()["latest_probe"]
        self.assertNotEqual(latest["state"], "completed")
        self.assertEqual(latest["meaning"], receipt["meaning"])

    def test_rejected_and_unknown_probe_receipts_do_not_claim_credential_acceptance(self):
        for status, error_type in ((400, "invalid_request_error"), (401, "authentication_error")):
            with self.subTest(http_status=status):
                self.fake.scenario = {"kind": "http_error", "status": status, "type": error_type}
                instance = self.room("probe-http-" + str(status))
                receipt = adapter.run_probe(instance)
                self.assertEqual((receipt["state"], receipt["http_status"]), ("rejected_before_generation", status))
                self.assert_unsuccessful_probe_is_candid(receipt, instance)
        connection = mock.Mock()
        connection.getresponse.side_effect = OSError("synthetic lost response")
        instance = self.room("probe-unknown")
        with mock.patch.object(adapter, "_open_connection", return_value=connection):
            receipt = adapter.run_probe(instance)
        self.assertEqual((receipt["state"], receipt["error_code"]), ("unknown_delivery", "transport_response_failed"))
        connection.request.assert_called_once()
        self.assert_unsuccessful_probe_is_candid(receipt, instance)
        self.assertEqual(len(self.fake.requests), 2)

    def test_probe_and_health_name_the_backend_and_redact_provider_echoes(self):
        self.fake.scenario.update(usage_value={"prompt_tokens": 42, fixtures.SYNTHETIC_KEY: 100})
        receipt = adapter.run_probe(self.adapter)
        self.assertEqual((receipt["state"], receipt["backend"]), ("completed", "deepinfra"))
        self.assertEqual(receipt["endpoint"]["path"], "/v1/openai/chat/completions")
        self.assertEqual(receipt["requested"]["reasoning_effort"], "max")
        self.assertEqual(receipt["requested"]["request_syntax"], "openai_reasoning_effort")
        self.assertEqual(receipt["usage"], {"prompt_tokens": 42})
        self.assertFalse(receipt["expected_answer_matched"])
        with mock.patch.object(adapter, "read_api_key", side_effect=AssertionError("health must not read a key")):
            health = self.adapter.health()
        self.assertEqual(health["latest_probe"]["backend"], "deepinfra")
        self.assertEqual(health["endpoint"]["host"], "api.deepinfra.com")
        self.assertNotIn(fixtures.SYNTHETIC_KEY, json.dumps([receipt, health]))
        for artifact in (self.home / "deepseek" / "probes").rglob("*"):
            if artifact.is_file():
                self.assertNotIn(fixtures.SYNTHETIC_KEY.encode(), artifact.read_bytes())

    def test_hosted_mcp_health_and_submit_keep_the_same_catalog(self):
        messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "deepseek_health", "arguments": {}}},
                    {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "deepseek_ask", "arguments": {
                        "question": "A tiny fake request", "request_id": "mcp-1", "effort": "none"}}}]
        process = subprocess.run([sys.executable, str(self.boot), str(self.fake.port), fixtures.ADAPTER, "serve",
                                  "--home", str(self.home), "--room", self.room_id, "--config", str(self.config_path)],
                                 input="".join(json.dumps(m) + "\n" for m in messages), text=True, capture_output=True, timeout=30)
        self.assertEqual(process.returncode, 0, process.stderr)
        replies = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual([r["id"] for r in replies], [1, 2, 3, 4])
        self.assertEqual({t["name"] for t in replies[1]["result"]["tools"]}, set(adapter.TOOLS))
        self.assertEqual(replies[2]["result"]["structuredContent"]["backend"], "deepinfra")
        self.assertEqual(replies[3]["result"]["structuredContent"]["state"], "completed")
        self.assertEqual(len(self.fake.requests), 1)
        self.assertNotIn(fixtures.SYNTHETIC_KEY, process.stdout + process.stderr)


class DeepInfraConfigTests(unittest.TestCase):
    def test_hosted_defaults_and_explicit_examples_preserve_full_requested_budgets(self):
        home = "/private/provider-fixture"
        config = adapter.validate_config({"backend": "deepinfra"}, home)
        self.assertEqual((config["model"], config["reasoning_effort"], config["max_tokens"], config["context_tokens"]),
                         ("deepseek-ai/DeepSeek-V4.1-Flash", "max", 131072, 1048576))
        self.assertEqual(config["api_key_file"], home + "/secrets/deepinfra-api-key")
        self.assertEqual(config["request_timeout_seconds"], -(-131072 // 40) + 60)
        body = adapter.build_body(config, adapter.lane_parameters(config, "deep"), "Task")
        self.assertEqual((body["max_tokens"], body["reasoning_effort"]), (131072, "max"))
        example = json.loads((fixtures.ROOT / "examples/deepinfra-provider.example.json").read_text())
        self.assertEqual(adapter.validate_config(example, home), config)
        old = adapter.validate_config({}, home)
        old.pop("backend")
        self.assertEqual(adapter.validate_config(old, home), {"backend": "official", **old})
        self.assertEqual(old["max_tokens"], 393216)

    def test_contradictory_transports_keys_models_and_budgets_are_rejected(self):
        for override in ({"backend": "unknown"}, {"backend": None}, {"base_url": "https://api.deepseek.com"},
                         {"base_url": "https://api.deepinfra.com/v1"}, {"base_url": "http://127.0.0.1"},
                         {"api_key_file": "/private/secrets/deepseek-api-key"}, {"model": adapter.DEFAULT_MODEL},
                         {"model": "org/name/extra"}, {"model": "org/a name"}, {"request_timeout_seconds": 3336},
                         {"api_key": "sk-synthetic"}, {"stream_options": {}}, {"max_tokens": True}):
            with self.subTest(override=override), self.assertRaises(adapter.AdapterError) as rejected:
                adapter.validate_config({"backend": "deepinfra", **override}, "/private/provider-fixture")
            self.assertEqual(rejected.exception.code, "config_invalid")
        for override in ({"base_url": "https://api.deepinfra.com/v1/openai"}, {"model": adapter.DEEPINFRA_MODEL},
                         {"api_key_file": "/private/secrets/deepinfra-api-key"}):
            with self.subTest(official=override), self.assertRaises(adapter.AdapterError):
                adapter.validate_config(override, "/private/provider-fixture")

    def test_key_cli_routes_to_the_selected_backend_without_capturing_a_secret(self):
        for backend in ("deepinfra", "official"):
            output = io.StringIO()
            with mock.patch.object(adapter, "set_key", return_value={"saved": True}) as setter, contextlib.redirect_stdout(output):
                self.assertEqual(adapter.main(["set-key", "--home", "/private/provider-fixture", "--backend", backend]), 0)
            self.assertEqual(setter.call_args.args[1], f"/private/provider-fixture/secrets/{backend if backend == 'deepinfra' else 'deepseek'}-api-key")
            self.assertEqual(json.loads(output.getvalue())["backend"], backend)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as rejected:
            adapter.main(["set-key", "--home", "/private/provider-fixture", "--backend", "deepinfra", "--config", "/private/config.json"])
        self.assertEqual(rejected.exception.code, 2)


class DeepInfraWiringTests(WiringFixture):
    def test_new_rooms_pin_hosted_settings_and_existing_official_rooms_stay_unchanged(self):
        self.configure("deepseek")
        official_id, official_root = self.open_room("Saved official provider")
        before = {str(p.relative_to(official_root)): p.read_bytes() for p in official_root.rglob("*") if p.is_file()}
        self.provider_config.write_text(json.dumps({"backend": "deepinfra"}))
        self.configure("deepseek")
        hosted_id, hosted_root = self.open_room("New hosted provider")
        snapshot = json.loads((hosted_root / "profiles/deepseek.json").read_text())
        self.assertEqual((snapshot["backend"], snapshot["model"], snapshot["max_tokens"]), ("deepinfra", adapter.DEEPINFRA_MODEL, 131072))
        self.assertEqual(snapshot["reasoning_effort"], "max")
        self.assertTrue(snapshot["api_key_file"].endswith("/secrets/deepinfra-api-key"))
        with mock.patch.object(adapter, "read_api_key", side_effect=AssertionError("diagnostics must not read a key")):
            doctor = self.service.room_doctor()
            hosted = self.service.room_status(hosted_id)["delegate_jobs"]
            official = self.service.room_status(official_id)["delegate_jobs"]
        self.assertEqual((doctor["deepseek"]["backend"], hosted["backend"], official["backend"]), ("deepinfra", "deepinfra", "official"))
        self.assertEqual(hosted["model"], adapter.DEEPINFRA_MODEL)
        self.assertEqual(official["model"], adapter.DEFAULT_MODEL)
        settings = json.loads((hosted_root / "settings.json").read_text())
        inventory = settings["provider_inventory"]
        verified = {path: Path(path).read_bytes() for path in inventory["files"]}
        pinned, error = implementation._pinned_delegate_settings(inventory, verified)
        self.assertIsNone(error)
        self.assertEqual((pinned["backend"], pinned["model"], pinned["max_tokens"]), ("deepinfra", adapter.DEEPINFRA_MODEL, 131072))
        self.assertEqual(pinned["base_url"], "https://api.deepinfra.com/v1/openai")
        for relative, data in before.items():
            self.assertEqual((official_root / relative).read_bytes(), data, relative)
        self.assertEqual(self.calls(), [], "setup and diagnostics never launch a model")


if __name__ == "__main__":
    unittest.main()
