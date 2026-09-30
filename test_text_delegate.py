"""Profile, transport and worker tests for the standalone text delegate."""

import ast
import io
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import sys
import tempfile
import time
import unittest
from unittest import mock

import deepseek_adapter as legacy
import text_delegate as delegate
from test_deepseek_adapter import FAST, FakeDeepSeek, USAGE


def make_profile(syntax="openai_reasoning_effort", kind="https", host="api.example.com", port=443, auth=None, **overrides):
    value = {
        "profile_version": 1,
        "id": "fixture",
        "transport": {"kind": kind, "host": host, "port": port, "path": "/v1/chat/completions"},
        "model": legacy.DEFAULT_MODEL,
        "request_syntax": syntax,
        "reasoning_effort": "max",
        "auth": auth or {"kind": "bearer_file", "api_key_file": None},
        "limits": {**FAST, "context_basis": "TOKEN"},
    }
    value.update(overrides)
    if syntax == "openai_plain" and "reasoning_effort" not in overrides:
        value.pop("reasoning_effort")
    return value


def write_profile(path, value):
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    path.write_bytes(raw)
    return raw


class ProfileValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="text-delegate-profile-test-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve() / "home"

    def assert_invalid(self, value):
        with self.assertRaises(delegate.AdapterError) as caught:
            delegate.validate_config(value, self.home)
        self.assertEqual(caught.exception.code, "config_invalid")

    def test_valid_https_and_loopback_profiles_normalize_defaults(self):
        https = delegate.validate_config(make_profile(), self.home)
        self.assertEqual(https["profile_id"], "fixture")
        self.assertEqual(https["label"], "fixture")
        self.assertEqual(https["transport"], {"kind": "https", "host": "api.example.com", "port": 443, "path": "/v1/chat/completions"})
        self.assertEqual(https["api_key_file"], str(self.home / "secrets" / "fixture-api-key"))
        self.assertEqual(https["max_concurrent_per_host"], 2)
        loopback = delegate.validate_config(
            make_profile("openai_plain", "loopback_http", "127.0.0.1", 8000, {"kind": "none"}), self.home)
        self.assertEqual(loopback["max_concurrent_per_host"], 1)
        self.assertIsNone(loopback["api_key_file"])
        self.assertIsNone(loopback["reasoning_effort"])
        self.assertEqual(loopback["ask_effort"], "none")
        self.assertEqual(https["max_context_files"], delegate.DEFAULTS["max_context_files"])

    def test_load_config_returns_the_digest_of_the_exact_profile_bytes(self):
        path = Path(self.temp.name) / "profile.json"
        raw = write_profile(path, make_profile())
        normalized, digest = delegate.load_config(path, self.home)
        self.assertEqual(digest, delegate.sha(raw))
        self.assertEqual(normalized["profile_id"], "fixture")

    def test_unknown_fields_bad_profile_id_and_credential_like_fields_are_refused(self):
        cases = []
        cases.append(("top-level", lambda value: value.update(extra=True)))
        cases.append(("transport", lambda value: value["transport"].update(extra=True)))
        cases.append(("auth", lambda value: value["auth"].update(extra=True)))
        cases.append(("limits", lambda value: value["limits"].update(extra=True)))
        cases.append(("credential field", lambda value: value.update(API_KEY="secret")))
        cases.append(("nested credential field", lambda value: value["auth"].update(private_key="secret")))
        cases.append(("bad id", lambda value: value.update(id="Not-valid")))
        for label, change in cases:
            with self.subTest(label=label):
                value = make_profile()
                change(value)
                self.assert_invalid(value)

    def test_transport_validation_refuses_bad_hosts_ports_and_paths(self):
        cases = []
        cases.append(("https ip", lambda value: value["transport"].update(host="127.0.0.1")))
        cases.append(("localhost loopback", lambda value: value["transport"].update(kind="loopback_http", host="localhost")))
        cases.append(("non-loopback ip", lambda value: value["transport"].update(kind="loopback_http", host="10.0.0.1")))
        cases.append(("missing port", lambda value: value["transport"].pop("port")))
        cases.append(("boolean port", lambda value: value["transport"].update(port=True)))
        cases.append(("zero port", lambda value: value["transport"].update(port=0)))
        cases.append(("large port", lambda value: value["transport"].update(port=65536)))
        cases.append(("path without slash", lambda value: value["transport"].update(path="v1/chat")))
        cases.append(("query path", lambda value: value["transport"].update(path="/chat?x=1")))
        cases.append(("fragment path", lambda value: value["transport"].update(path="/chat#fragment")))
        for label, change in cases:
            with self.subTest(label=label):
                value = make_profile()
                change(value)
                self.assert_invalid(value)

    def test_auth_syntax_limits_and_timeout_validation(self):
        cases = []
        cases.append(("none over https", lambda value: value.update(auth={"kind": "none"})))
        cases.append(("relative key path", lambda value: value.update(auth={"kind": "bearer_file", "api_key_file": "secrets/key"})))
        cases.append(("unknown syntax", lambda value: value.update(request_syntax="unsupported")))
        cases.append(("plain reasoning effort", lambda value: value.update(request_syntax="openai_plain")))
        cases.append(("plain low ask effort", lambda value: (value.update(request_syntax="openai_plain", ask_effort="low"),
                                                              value.pop("reasoning_effort", None))))
        required_limits = ("max_tokens", "context_tokens", "min_tokens_per_second", "request_timeout_seconds", "context_basis")
        for name in required_limits:
            cases.append(("missing " + name, lambda value, field=name: value["limits"].pop(field)))
        cases.append(("timeout too small", lambda value: value["limits"].update(request_timeout_seconds=1)))
        for label, change in cases:
            with self.subTest(label=label):
                value = make_profile()
                change(value)
                self.assert_invalid(value)

    def test_plain_syntax_rejects_low_effort_at_ask_time(self):
        path = Path(self.temp.name) / "plain-profile.json"
        write_profile(path, make_profile("openai_plain", "loopback_http", "127.0.0.1", 8000, {"kind": "none"}))
        instance = delegate.Adapter(self.home, "room", path)
        with self.assertRaises(delegate.AdapterError) as caught:
            instance.ask("question", "request-1", effort="low")
        self.assertEqual(caught.exception.code, "effort_invalid")
        self.assertEqual(delegate.lane_parameters(instance.config, "deep")["thinking"], "not_requested")

    def test_set_key_refuses_profiles_without_authentication(self):
        path = Path(self.temp.name) / "plain-profile.json"
        write_profile(path, make_profile("openai_plain", "loopback_http", "127.0.0.1", 8000, {"kind": "none"}))
        stderr = io.StringIO()
        with mock.patch.object(sys, "stderr", stderr):
            status = delegate.main(["set-key", "--home", str(self.home), "--config", str(path)])
        self.assertEqual(status, 2)
        self.assertIn('"code": "config_invalid"', stderr.getvalue())

    def test_vllm_documented_profile_passes_validation(self):
        reference = Path(__file__).resolve().parent / "docs" / "reference" / "text-delegate.md"
        blocks = re.findall(r"```json\n(.*?)\n```", reference.read_text(encoding="utf-8"), re.DOTALL)
        self.assertTrue(blocks)
        profile = json.loads(blocks[-1])
        config = delegate.validate_config(profile, self.home)
        self.assertEqual(config["context_tokens"], 32768)
        self.assertEqual(config["request_timeout_seconds"], 470)


class RequestBodyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="text-delegate-body-test-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()

    def test_reasoning_request_bodies_match_the_existing_adapter(self):
        user_text = "self-contained task"
        for syntax, backend, model in (
                ("deepseek_thinking", "official", legacy.DEFAULT_MODEL),
                ("openai_reasoning_effort", "deepinfra", legacy.DEEPINFRA_MODEL)):
            with self.subTest(syntax=syntax):
                profile = make_profile(syntax)
                profile["model"] = model
                normalized = delegate.validate_config(profile, self.home)
                old = legacy.validate_config({"backend": backend, **FAST}, self.home)
                for lane in ("deep", "ask"):
                    new_parameters = delegate.lane_parameters(normalized, lane)
                    old_parameters = legacy.lane_parameters(old, lane)
                    self.assertEqual(new_parameters, old_parameters)
                    self.assertEqual(delegate.build_body(normalized, new_parameters, user_text),
                                     legacy.build_body(old, old_parameters, user_text))

    def test_openai_plain_body_has_only_the_supported_fields(self):
        config = delegate.validate_config(
            make_profile("openai_plain", "loopback_http", "127.0.0.1", 8000, {"kind": "none"}), self.home)
        for lane in ("deep", "ask"):
            parameters = delegate.lane_parameters(config, lane)
            body = delegate.build_body(config, parameters, "task")
            self.assertEqual(set(body), {"model", "messages", "stream", "max_tokens", "stream_options"})
            self.assertEqual(body["model"], config["model"])
            self.assertEqual(body["messages"], [{"role": "system", "content": delegate.FRAMING},
                                                {"role": "user", "content": "task"}])
            self.assertIs(body["stream"], True)
            self.assertEqual(body["max_tokens"], parameters["max_tokens"])
            self.assertEqual(body["stream_options"], {"include_usage": True})
            self.assertNotIn("reasoning_effort", body)
            self.assertNotIn("thinking", body)
            self.assertEqual(parameters["thinking"], "not_requested")
            self.assertIsNone(parameters["reasoning_effort"])

    def test_reasoning_aliases_are_not_concatenated(self):
        config = delegate.validate_config(make_profile(), self.home)
        artifacts_fd = os.open(self.temp.name, os.O_RDONLY | os.O_DIRECTORY)
        stream = delegate._Stream(config, artifacts_fd, None, {})
        reasoning = "é"
        delta = {"reasoning_content": reasoning, "reasoning": reasoning}
        try:
            stream.line(b"data: " + json.dumps({"choices": [{"delta": delta}]}).encode("utf-8"))
            self.assertEqual(stream.reasoning_bytes, len(reasoning.encode("utf-8")))
        finally:
            try:
                stream.close()
            finally:
                os.close(artifacts_fd)
        self.assertEqual((Path(self.temp.name) / "reasoning").read_bytes(), reasoning.encode("utf-8"))


class TransportAndWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="text-delegate-worker-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.home = self.base / "home"
        self.home.mkdir(mode=0o700)
        self.fake = FakeDeepSeek()
        self.addCleanup(self.fake.close)
        self.profile_path = self.base / "profile.json"
        self.write_loopback_profile()
        self.export_dir = self.base / "granted-exports"
        self.export_dir.mkdir(mode=0o700)
        self.room_root = self.base / "room-root"
        self.room_root.mkdir(mode=0o700)
        adapter_path = Path(delegate.__file__).resolve()
        inventory = {
            "provider": "text_delegate",
            "profile_id": "fixture",
            "room_id": "worker-room",
            "files": {
                str(adapter_path): delegate.sha(adapter_path.read_bytes()),
                str(self.profile_path.resolve()): delegate.sha(self.profile_path.read_bytes()),
            },
            "export_dir": str(self.export_dir),
        }
        (self.room_root / "settings.json").write_text(json.dumps({"provider_inventory": inventory}), encoding="utf-8")
        self.adapter = delegate.Adapter(self.home, "worker-room", self.profile_path, room_root=self.room_root)

    def write_loopback_profile(self):
        profile = make_profile("openai_plain", "loopback_http", "127.0.0.1", self.fake.port, {"kind": "none"})
        raw = json.loads(json.dumps(profile))
        raw["transport"]["path"] = "/v1/chat/completions"
        write_profile(self.profile_path, raw)

    def run_to_terminal(self, request_id, scenario):
        self.fake.scenario = {"kind": "ok", "model": legacy.DEFAULT_MODEL, **scenario}
        submitted = self.adapter.submit("Return a short answer for " + request_id, request_id)
        deadline = time.monotonic() + 12
        while True:
            value = self.adapter.status(submitted["job_id"], wait=True, timeout_s=2)
            if value["terminal"] or time.monotonic() >= deadline:
                return value

    def test_loopback_worker_streams_usage_and_reasoning_without_auth(self):
        only_usage = self.run_to_terminal("usage-only", {"usage": "separate", "reasoning_key": "reasoning"})
        self.assertEqual(only_usage["state"], "completed")
        self.assertEqual(only_usage["usage_source"], "usage_only_chunk")
        self.assertEqual(only_usage["usage"], USAGE)
        first_result = self.adapter.result(only_usage["job_id"])
        self.assertEqual(self.adapter.export_dir, self.export_dir)
        self.assertEqual(Path(first_result["content_path"]).parent, self.export_dir)
        self.assertNotEqual(self.export_dir.name, self.adapter.room_id)
        self.assertEqual(self.adapter._export_bytes(only_usage["job_id"], self.adapter.room_id),
                         Path(first_result["content_path"]).stat().st_size)
        self.assertNotIn("PRIVATE_REASONING", first_result["text"])
        job_dir = self.home / "delegates" / "fixture" / "jobs" / only_usage["job_id"]
        self.assertIn(b"PRIVATE_REASONING", (job_dir / "reasoning").read_bytes())
        self.assertNotIn(b"PRIVATE_REASONING", Path(first_result["content_path"]).read_bytes())

        missing = self.run_to_terminal("missing-usage", {"usage": "none", "reasoning_key": "reasoning"})
        self.assertEqual(missing["state"], "completed")
        self.assertIsNone(missing["usage"])
        self.assertEqual(missing["usage_source"], "missing")
        self.assertEqual(self.adapter.result(missing["job_id"])["state"], "completed")

        self.fake.scenario = {"kind": "ok", "model": legacy.DEFAULT_MODEL}
        probe = delegate.run_probe(self.adapter, expected="Hello, wörld OK")
        self.assertEqual(probe["kind"], "delegate_probe")
        self.assertEqual(probe["endpoint"], {"kind": "loopback_http", "host": "127.0.0.1",
                                             "port": self.fake.port, "path": "/v1/chat/completions"})
        self.assertEqual(probe["profile_id"], "fixture")
        self.assertTrue(probe["expected_answer_matched"])

        self.assertEqual(self.fake.requests[0]["path"], "/v1/chat/completions")
        self.assertNotIn("Authorization", self.fake.requests[0]["headers"])
        root = self.home / "delegates" / "fixture"
        self.assertTrue((root / "ledger.sqlite3").is_file())
        self.assertTrue((root / "jobs").is_dir())
        self.assertTrue((root / "probes").is_dir())
        self.assertTrue(self.export_dir.is_dir())
        self.assertFalse((self.home / "deepseek").exists())
        health = self.adapter.health()
        self.assertEqual(health["provider"], "text_delegate")
        self.assertEqual(health["profile_id"], "fixture")
        self.assertEqual(health["label"], "fixture")
        self.assertEqual(health["transport"]["port"], self.fake.port)
        self.assertEqual(health["request_syntax"], "openai_plain")
        self.assertEqual(health["auth_kind"], "none")

    def test_default_export_dir_is_selected_without_creating_it(self):
        fallback_home = self.base / "fallback-home"
        fallback_home.mkdir(mode=0o700)
        adapter = delegate.Adapter(fallback_home, "fallback-room", self.profile_path)
        expected = fallback_home / "delegates" / "fixture" / "exports" / "fallback-room"
        self.assertEqual(adapter.export_dir, expected)
        self.assertFalse(expected.exists())

    def test_worker_refuses_queued_job_when_profile_changes(self):
        with mock.patch.object(self.adapter, "_spawn_worker", lambda job_id, job_fd, lease_fd: None):
            submitted = self.adapter.submit("Do not send with a changed profile", "profile-change")

        profile = json.loads(self.profile_path.read_text(encoding="utf-8"))
        profile["model"] = "fixture/changed-model"
        write_profile(self.profile_path, profile)
        changed_adapter = delegate.Adapter(self.home, "worker-room", self.profile_path, room_root=self.room_root)
        job_fd = changed_adapter._job_fd(submitted["job_id"])
        lease_fd = os.open("worker.lock", os.O_RDWR | os.O_NOFOLLOW, dir_fd=job_fd)
        try:
            changed_adapter.run_worker(submitted["job_id"], lease_fd)
        finally:
            os.close(lease_fd)
            os.close(job_fd)

        row = changed_adapter.ledger.job(submitted["job_id"], "worker-room")
        self.assertEqual((row["state"], row["remote_outcome"], row["error_code"], row["possibly_billed"]),
                         (delegate.NOT_STARTED, "not_sent", "profile_changed", 0))
        self.assertEqual(self.fake.requests, [])

    def test_loopback_redirect_is_refused_after_send(self):
        value = self.run_to_terminal("redirect", {"kind": "redirect"})
        self.assertEqual((value["state"], value["error_code"]), ("failed_after_send", "redirect_refused"))
        self.assertEqual(len(self.fake.requests), 1)
        self.assertNotIn("Authorization", self.fake.requests[0]["headers"])

    def test_resolve_job_persists_resolution_and_unblocks_room(self):
        failed = self.run_to_terminal("resolve", {"kind": "redirect"})
        self.assertEqual(failed["state"], "failed_after_send")

        resolution = delegate.resolve_job(
            self.home, self.profile_path, "worker-room", failed["job_id"], "accept the uncertain outcome", interactive=True)
        self.assertTrue(resolution["resolved"])
        receipt = self.home / "delegates" / "fixture" / "jobs" / failed["job_id"] / "resolution.json"
        self.assertTrue(receipt.is_file())
        self.assertEqual(json.loads(receipt.read_text(encoding="utf-8"))["note"], "accept the uncertain outcome")

        self.fake.scenario = {"kind": "ok", "model": legacy.DEFAULT_MODEL}
        submitted = self.adapter.submit("New task after resolution", "after-resolution")
        completed = self.adapter.status(submitted["job_id"], wait=True, timeout_s=12)
        self.assertEqual(completed["state"], "completed")


class HttpsAndSurfaceTests(unittest.TestCase):
    def test_https_connection_uses_verifying_context_and_configured_endpoint_and_bearer(self):
        config = delegate.validate_config(make_profile(), Path("/tmp").resolve())
        with mock.patch.object(delegate.http.client, "HTTPSConnection") as https, \
                mock.patch.object(delegate, "tls_context", wraps=delegate.tls_context) as context_factory:
            delegate._open_connection(config["transport"], 7.5)
        context = https.call_args.kwargs["context"]
        self.assertTrue(context_factory.called)
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertGreaterEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)
        https.assert_called_once_with("api.example.com", 443, context=context, timeout=7.5)

        class Response:
            status = 302

        class Connection:
            sock = None

            def __init__(self):
                self.headers = None

            def connect(self):
                pass

            def request(self, method, path, body=None, headers=None):
                self.headers = headers

            def getresponse(self):
                return Response()

            def close(self):
                pass

        connection = Connection()
        config["transport"] = {"kind": "https", "host": "api.example.com", "port": 443, "path": "/v1/chat/completions"}
        artifacts = Path(tempfile.mkdtemp(prefix="text-delegate-https-artifacts-"))
        self.addCleanup(lambda: shutil.rmtree(artifacts))
        fd = os.open(artifacts, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with mock.patch.object(delegate, "_open_connection", return_value=connection):
                outcome = delegate.execute_request(config, "synthetic-token", {"model": config["model"]}, fd, time.monotonic() + 5)
        finally:
            os.close(fd)
        self.assertEqual(outcome["error_code"], "redirect_refused")
        self.assertEqual(connection.headers["Authorization"], "Bearer synthetic-token")

    def test_inventory_identity_tools_and_stdlib_only_imports(self):
        root = Path(tempfile.mkdtemp(prefix="text-delegate-inventory-test-"))
        self.addCleanup(lambda: shutil.rmtree(root))
        settings = root / "settings.json"
        inventory = {"provider": "text_delegate", "profile_id": "fixture", "room_id": "room",
                     "files": {}, "export_dir": str(root / "exports")}
        settings.write_text(json.dumps({"provider_inventory": inventory}), encoding="utf-8")
        self.assertEqual(delegate.read_inventory(root, "room", "fixture"), inventory)
        with self.assertRaises(delegate.AdapterError):
            delegate.read_inventory(root, "room", "other")
        inventory["provider"] = "other"
        settings.write_text(json.dumps({"provider_inventory": inventory}), encoding="utf-8")
        with self.assertRaises(delegate.AdapterError):
            delegate.read_inventory(root, "room", "fixture")

        initialized = delegate.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                       "params": {"protocolVersion": "2024-11-05"}}, None)
        self.assertEqual(initialized["result"]["serverInfo"]["name"], "text-delegate")
        listed = delegate.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, None)
        self.assertEqual({tool["name"] for tool in listed["result"]["tools"]},
                         {"delegate_submit", "delegate_ask", "delegate_status", "delegate_result", "delegate_cancel", "delegate_health"})
        tree = ast.parse(Path(delegate.__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".", 1)[0])
        self.assertTrue(imported <= sys.stdlib_module_names, sorted(imported - sys.stdlib_module_names))


if __name__ == "__main__":
    unittest.main()
