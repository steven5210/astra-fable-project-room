"""DeepSeek adapter contract and lifecycle tests: fake loopback HTTP/SSE and synthetic keys only, no network."""

import contextlib
import fcntl
import http.client
import http.server
import json
import os
from pathlib import Path
import subprocess
import sys
import stat
import tempfile
import threading
import time
import unittest
import uuid
from unittest import mock

import deepseek_adapter as adapter

ROOT = Path(__file__).resolve().parent
ADAPTER = str(ROOT / "deepseek_adapter.py")
SYNTHETIC_KEY = "sk-synthetic-fixture-key-0123456789abcdef"
# Transport injection for subprocess workers and MCP servers: replaces the TLS connection class with a loopback
# fake and rewrites worker argv so that the injection follows the detached worker. Pure Python, test-only.
BOOT = '''import http.client, os, runpy, stat, subprocess, sys
BOOT = os.path.abspath(sys.argv[0]); PORT = sys.argv.pop(1); ADAPTER = sys.argv.pop(1)
FAULT = os.environ.get("DEEPSEEK_FIXTURE_FAULT")
class Fake(http.client.HTTPConnection):
    def __init__(self, host, port=None, timeout=None, context=None, **kw):
        super().__init__("127.0.0.1", int(PORT), timeout=timeout)
    def connect(self):
        if FAULT == "log_spam":  # a chatty worker: about 1.8 MB through whatever sys.stderr is at that moment
            sys.stderr.write("spam " * 360000 + "\\n")
        super().connect()
http.client.HTTPSConnection = Fake
if FAULT == "fsync_directories":  # every directory fsync fails inside the worker (export publication, metadata writes)
    _fsync = os.fsync
    def fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(5, "synthetic directory fsync failure")
        return _fsync(fd)
    os.fsync = fsync
_Popen = subprocess.Popen
class Popen(_Popen):
    def __init__(self, args, *a, **kw):
        if isinstance(args, (list, tuple)) and len(args) > 2 and args[1] == ADAPTER and args[2] == "worker":
            args = [args[0], BOOT, PORT, *args[1:]]
        super().__init__(args, *a, **kw)
subprocess.Popen = Popen
runpy.run_path(ADAPTER, run_name="__main__")
'''
# A fast bounded configuration for lifecycle tests; the production defaults are exercised separately.
FAST = {"max_tokens": 64, "ask_max_tokens": 16, "context_tokens": 4096, "max_input_bytes": 2048, "min_tokens_per_second": 64,
        "connect_margin_seconds": 1, "request_timeout_seconds": 4, "read_idle_seconds": 2, "max_context_file_bytes": 1024,
        "max_content_bytes": 4096, "max_reasoning_bytes": 4096, "max_wire_bytes": 2 * 1048576, "max_wire_tail_bytes": 4096,
        "max_metadata_bytes": 131072, "max_sse_line_bytes": 65536, "max_retained_bytes": 4 * 1048576, "max_jobs": 100}


def chunk(delta, finish=None, usage=None, model=adapter.DEFAULT_MODEL, extra=None):
    value = {"id": "chatcmpl-fixture", "object": "chat.completion.chunk", "created": 1757000000, "model": model,
             "system_fingerprint": "fp_fixture", "choices": [{"index": 0, "delta": delta, "logprobs": None, "finish_reason": finish}]}
    if usage is not None:
        value["usage"] = usage
    if model is None:
        del value["model"]  # a stream that never identifies its model
    if extra:
        value.update(extra)
    return b"data: " + json.dumps(value, ensure_ascii=False).encode("utf-8") + b"\n\n"


USAGE = {"prompt_tokens": 42, "completion_tokens": 19, "total_tokens": 61, "prompt_tokens_details": {"cached_tokens": 0},
         "completion_tokens_details": {"reasoning_tokens": 17}, "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 42}


class FakeDeepSeek:
    """Loopback server speaking the DeepSeek chat-completion SSE shape under a scripted scenario."""

    def __init__(self):
        self.scenario = {"kind": "ok"}
        self.requests = []
        self.lock = threading.Lock()
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *unused):
                pass

            def do_POST(self):
                fake.handle(self)

        class Server(http.server.ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                pass  # a killed or cancelled client is expected in these tests

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def handle(self, request):
        length = int(request.headers.get("Content-Length") or 0)
        raw = request.rfile.read(length)
        record = {"path": request.path, "headers": dict(request.headers.items()), "raw": raw}
        try:
            record["body"] = json.loads(raw)
        except ValueError:
            record["body"] = None
        with self.lock:
            self.requests.append(record)
        scenario = dict(self.scenario)
        kind = scenario.get("kind", "ok")
        if kind == "hang":
            time.sleep(scenario.get("seconds", 6))
            kind = "ok"
        if kind == "redirect":
            request.send_response(302)
            request.send_header("Location", "https://redirect.example/elsewhere")
            request.send_header("Content-Length", "0")
            request.end_headers()
            return
        if kind == "http_error":
            payload = scenario.get("body")
            if payload is None:
                payload = json.dumps({"error": {"message": "PROVIDER_PROSE_MUST_NOT_LEAK", "type": scenario.get("type", "invalid_request_error"),
                                                "code": scenario.get("code")}}).encode()
            request.send_response(scenario["status"])
            request.send_header("Content-Type", scenario.get("content_type", "application/json"))
            request.send_header("Content-Length", str(len(payload)))
            request.end_headers()
            request.wfile.write(payload)
            return
        if kind == "non_stream":
            payload = json.dumps({"id": "x", "object": "chat.completion", "choices": []}).encode()
            request.send_response(200)
            request.send_header("Content-Type", "application/json")
            request.send_header("Content-Length", str(len(payload)))
            request.end_headers()
            request.wfile.write(payload)
            return
        request.send_response(200)
        request.send_header("Content-Type", "text/event-stream; charset=utf-8")
        request.send_header("Transfer-Encoding", "chunked")
        request.end_headers()
        try:
            for piece in self.pieces(scenario):
                request.wfile.write(b"%x\r\n%s\r\n" % (len(piece), piece))
                request.wfile.flush()
                if scenario.get("slow"):
                    time.sleep(scenario["slow"])
            if scenario.get("premature"):
                request.close_connection = True  # drop the socket without the chunked terminator: a premature EOF
                return
            request.wfile.write(b"0\r\n\r\n")
            request.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def pieces(self, scenario):
        model = scenario.get("model", adapter.DEFAULT_MODEL)
        if scenario.get("kind") == "big":
            count = scenario["tokens"]
            yield b": keep-alive\n\n"
            reasoning = count // 2
            for index in range(count):
                if index == reasoning:
                    pass
                delta = {"reasoning_content": "r"} if index < reasoning else {"content": "c"}
                yield chunk(delta, model=model)
            yield chunk({}, finish="stop", usage={"prompt_tokens": 10, "completion_tokens": count, "total_tokens": count + 10}, model=model)
            yield b"data: [DONE]\n\n"
            return
        yield b": keep-alive\n\n"
        yield b"\n"
        yield chunk({"role": "assistant", "content": "", "reasoning_content": ""}, model=model)
        yield chunk({"reasoning_content": "PRIVATE_REASONING_MUST_NOT_LEAK "}, model=model)
        if scenario.get("error_in_stream"):
            yield b"data: " + json.dumps({"error": {"message": "PROVIDER_PROSE_MUST_NOT_LEAK", "type": scenario.get("error_type", "server_error")}}).encode() + b"\n\n"
            return
        if scenario.get("malformed"):
            yield b"data: {not json\n\n"
            return
        if scenario.get("tool_calls"):
            yield chunk({"tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "run", "arguments": "{}"}}]}, model=model)
            return
        first = chunk({"content": "Hello, wörld"}, model=model)
        cut = first.index("ö".encode("utf-8")) + 1  # split the two-byte character across two writes
        yield first[:cut]
        yield first[cut:]
        if scenario.get("premature"):
            yield chunk({"content": " partial"}, model=model)
            return
        finish = scenario.get("finish", "stop")
        placement = scenario.get("usage", "final")
        if scenario.get("omit_finish"):
            yield chunk({"content": " " + scenario.get("answer", "OK")}, model=model)
            yield b"data: [DONE]\n\n"
            return
        yield chunk({"content": " " + scenario.get("answer", "OK")}, finish=finish, usage=scenario.get("usage_value", USAGE) if placement == "final" else None, model=model)
        if scenario.get("content_after_finish"):
            yield chunk({"content": "late"}, model=model)
        if placement == "separate":
            value = {"id": "chatcmpl-fixture", "object": "chat.completion.chunk", "created": 1757000000, "model": model, "choices": [], "usage": USAGE}
            yield b"data: " + json.dumps(value).encode() + b"\n\n"
        if not scenario.get("omit_done"):
            yield b"data: [DONE]\n\n"


def fake_connection(port):
    class Fake(http.client.HTTPConnection):
        def __init__(self, host, port_=None, timeout=None, context=None, **kwargs):
            super().__init__("127.0.0.1", port, timeout=timeout)
    return Fake


class AdapterFixture(unittest.TestCase):
    config_overrides = FAST

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="deepseek-adapter-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.home = self.base / "home"
        self.home.mkdir(mode=0o700)
        self.secrets = self.home / "secrets"
        self.secrets.mkdir(mode=0o700)
        self.key_path = self.secrets / "deepseek-api-key"
        self.key_path.write_text(SYNTHETIC_KEY + "\n")
        self.key_path.chmod(0o600)
        self.config_path = self.base / "deepseek.json"
        self.write_config(self.config_overrides)
        self.room_id = "fixture-room"
        self.export_dir = self.home / "deepseek" / "exports" / self.room_id
        self.export_dir.mkdir(parents=True, mode=0o700)
        self.export_dir.parent.chmod(0o700)
        (self.home / "deepseek").chmod(0o700)
        self.fake = FakeDeepSeek()
        self.addCleanup(self.fake.close)
        self.boot = self.base / "boot.py"
        self.boot.write_text(BOOT)
        self.worktree = self.base / "worktree"
        self.worktree.mkdir()
        (self.worktree / "module.py").write_text("def value():\n    return 1\n")
        self.env = mock.patch.dict(os.environ, {"PROJECT_ROOM_WORKTREE": str(self.worktree)})
        self.env.start()
        self.addCleanup(self.env.stop)
        # Every in-process request and every spawned worker goes to the loopback fake, never to the network.
        self.connection = mock.patch.object(adapter, "_open_connection",
                                            lambda host, port, context, timeout: fake_connection(self.fake.port)(host, port, timeout=timeout))
        self.connection.start()
        self.addCleanup(self.connection.stop)
        self.command = mock.patch.object(adapter, "worker_command", lambda: [sys.executable, str(self.boot), str(self.fake.port), ADAPTER])
        self.command.start()
        self.addCleanup(self.command.stop)
        self.adapter = adapter.Adapter(self.home, self.room_id, self.config_path)

    def write_config(self, overrides):
        self.config_path.write_text(json.dumps(overrides))

    def wait(self, job_id, seconds=15, instance=None):
        instance = instance or self.adapter
        deadline = time.monotonic() + seconds
        while True:
            value = instance.status(job_id, True, 1)
            if value["terminal"] or time.monotonic() > deadline:
                return value

    def submit(self, task="Write the tests.", request_id="req-1", **kwargs):
        return self.adapter.submit(task, request_id, **kwargs)

    def room(self, name):
        """A second isolated room on the same host: its own export directory and adapter instance."""
        (self.home / "deepseek" / "exports" / name).mkdir(parents=True, mode=0o700, exist_ok=True)
        return adapter.Adapter(self.home, name, self.config_path)

    def run_to_terminal(self, instance, task, request_id, scenario, **kwargs):
        self.fake.scenario = scenario
        submitted = instance.submit(task, request_id, **kwargs)
        return self.wait(submitted["job_id"], instance=instance)

    def job_dir(self, job_id):
        return self.home / "deepseek" / "jobs" / job_id


class TransportContractTests(AdapterFixture):
    def test_request_schema_exact_model_pinned_settings_and_completed_export(self):
        submitted = self.submit()
        self.assertEqual((submitted["state"], submitted["duplicate"], submitted["lane"]), ("queued", False, "deep"))
        terminal = self.wait(submitted["job_id"])
        self.assertEqual(terminal["state"], "completed", terminal)
        request = self.fake.requests[0]
        self.assertEqual(request["path"], "/chat/completions")
        self.assertEqual(request["headers"]["Authorization"], "Bearer " + SYNTHETIC_KEY)
        body = request["body"]
        self.assertEqual(body["model"], adapter.DEFAULT_MODEL)
        self.assertEqual(body["thinking"], {"type": "enabled"})
        self.assertEqual(body["reasoning_effort"], "max")
        self.assertEqual(body["max_tokens"], FAST["max_tokens"])
        self.assertTrue(body["stream"])
        self.assertNotIn("stream_options", body)
        for absent in ("temperature", "top_p", "tools", "presence_penalty", "frequency_penalty"):
            self.assertNotIn(absent, body)
        self.assertEqual([message["role"] for message in body["messages"]], ["system", "user"])
        self.assertIn("Write the tests.", body["messages"][1]["content"])
        self.assertEqual(terminal["observed_model"], adapter.DEFAULT_MODEL)
        self.assertEqual(terminal["finish_reason"], "stop")
        self.assertEqual(terminal["usage"]["completion_tokens_details"]["reasoning_tokens"], 17)
        self.assertEqual(terminal["usage_source"], "final_chunk")
        self.assertTrue(terminal["complete"])
        self.assertFalse(terminal["stops_room_lane"])
        self.assertTrue(terminal["possibly_billed"])
        result = self.adapter.result(submitted["job_id"])
        self.assertEqual(result["text"], "Hello, wörld OK")
        self.assertIsNone(result["next_offset"])
        export = Path(result["content_path"])
        self.assertEqual(export, self.export_dir / (submitted["job_id"] + ".md"))
        self.assertEqual(export.stat().st_mode & 0o777, 0o400)
        self.assertEqual(adapter.sha(export.read_bytes()), result["content_sha256"])
        self.assertEqual(sorted(path.name for path in self.export_dir.iterdir()), [export.name])
        job_dir = self.home / "deepseek" / "jobs" / submitted["job_id"]
        self.assertFalse((job_dir / "content").exists(), "one authoritative retained copy")
        self.assertIn("PRIVATE_REASONING_MUST_NOT_LEAK", (job_dir / "reasoning").read_text())
        for name in ("reasoning", "request.json", "request-text", "meta.json", "wire-tail", "worker.log"):
            self.assertEqual((job_dir / name).stat().st_mode & 0o777, 0o600, name)
        serialized = json.dumps([terminal, result, self.adapter.health()])
        for sentinel in ("PRIVATE_REASONING_MUST_NOT_LEAK", SYNTHETIC_KEY):
            self.assertNotIn(sentinel, serialized)
        self.assertNotIn(SYNTHETIC_KEY, (job_dir / "wire-tail").read_bytes().decode("utf-8", "replace"))
        self.assertEqual(terminal["reasoning_bytes"], len("PRIVATE_REASONING_MUST_NOT_LEAK "))
        duplicate = self.submit()
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["job_id"], submitted["job_id"])
        self.assertEqual(len(self.fake.requests), 1)

    def test_usage_placements_and_missing_usage_are_reported_truthfully(self):
        separate = self.run_to_terminal(self.room("usage-separate"), "Task A", "r1", {"kind": "ok", "usage": "separate"})
        self.assertEqual((separate["state"], separate["usage_source"], separate["usage"]["total_tokens"]), ("completed", "usage_only_chunk", 61))
        missing = self.run_to_terminal(self.room("usage-missing"), "Task B", "r1", {"kind": "ok", "usage": "missing"})
        self.assertEqual((missing["state"], missing["usage_source"], missing["usage"]), ("completed", "missing", None))

    def test_length_and_filter_finishes_are_truncated_never_completed(self):
        for finish in ("length", "content_filter", "insufficient_system_resource"):
            with self.subTest(finish=finish):
                instance = self.room("finish-" + finish.replace("_", "-"))
                terminal = self.run_to_terminal(instance, "Task " + finish, "r1", {"kind": "ok", "finish": finish})
                self.assertEqual((terminal["state"], terminal["finish_reason"], terminal["error_code"]), ("truncated", finish, "finish_" + finish))
                self.assertFalse(terminal["complete"])
                self.assertFalse(terminal["stops_room_lane"])
                self.assertIsNone(terminal["content_path"])
                with self.assertRaises(adapter.AdapterError) as refused:
                    instance.result(terminal["job_id"])
                self.assertEqual(refused.exception.code, "result_unavailable")
                self.assertTrue((self.job_dir(terminal["job_id"]) / "content").exists(), "incomplete content stays private")
                self.assertEqual(list((self.home / "deepseek" / "exports" / instance.room_id).iterdir()), [])

    def test_every_incomplete_or_contradictory_stream_is_never_completed(self):
        cases = [("premature", {"kind": "ok", "premature": True}, "failed_after_send", "stream_incomplete"),
                 ("omit-done", {"kind": "ok", "omit_done": True}, "failed_after_send", "stream_incomplete"),
                 ("omit-finish", {"kind": "ok", "omit_finish": True}, "failed_after_send", "finish_missing"),
                 ("malformed", {"kind": "ok", "malformed": True}, "failed_after_send", "malformed_stream"),
                 ("tool-calls", {"kind": "ok", "tool_calls": True}, "failed_after_send", "unsupported_tool_calls"),
                 ("model", {"kind": "ok", "model": "deepseek-v4-flash"}, "failed_after_send", "model_mismatch"),
                 ("model-absent", {"kind": "ok", "model": None}, "failed_after_send", "model_unverified"),
                 ("timeout-408", {"kind": "http_error", "status": 408}, "failed_after_send", "http_408_ambiguous"),
                 ("late", {"kind": "ok", "content_after_finish": True}, "failed_after_send", "content_after_finish"),
                 ("error", {"kind": "ok", "error_in_stream": True}, "failed_after_send", "provider_error_in_stream"),
                 ("json", {"kind": "non_stream"}, "failed_after_send", "unexpected_content_type"),
                 ("redirect", {"kind": "redirect"}, "failed_after_send", "redirect_refused"),
                 ("gateway", {"kind": "http_error", "status": 502, "body": b"<html>Bad gateway</html>", "content_type": "text/html"}, "failed_after_send", "http_502_unvalidated")]
        for name, scenario, state, code in cases:
            with self.subTest(case=name):
                instance = self.room("stream-" + name)
                terminal = self.run_to_terminal(instance, "Task " + name, "r1", scenario)
                self.assertEqual((terminal["state"], terminal["error_code"]), (state, code), terminal)
                self.assertFalse(terminal["complete"])
                self.assertTrue(terminal["possibly_billed"])
                self.assertEqual(terminal["remote_outcome"], "unknown")
                self.assertTrue(terminal["stops_room_lane"])
                self.assertIn("resolve --home", terminal["resolve_command"])
                if name in ("model", "model-absent"):
                    self.assertIsNone(terminal["observed_model"], "a differing or absent model identity is never recorded or inferred")
                if name == "timeout-408":
                    self.assertEqual((terminal["availability"], terminal["error_type"]), ("outcome_ambiguous", "invalid_request_error"))
                if name == "error":
                    self.assertEqual(terminal["error_type"], "server_error")
                with self.assertRaises(adapter.AdapterError) as refused:
                    instance.submit("A different task", "r2")
                self.assertEqual(refused.exception.code, "room_stopped")
                self.assertNotIn("PROVIDER_PROSE_MUST_NOT_LEAK", json.dumps([terminal, refused.exception.payload()]))
                self.assertEqual(list((self.home / "deepseek" / "exports" / instance.room_id).iterdir()), [])

    def test_server_failures_stay_potentially_billed_even_with_a_validated_envelope(self):
        for status, body in ((500, None), (503, None), (502, b"<html>Bad gateway</html>"), (504, b"")):
            with self.subTest(status=status):
                instance = self.room(f"server-{status}")
                scenario = {"kind": "http_error", "status": status, "type": "server_error"}
                if body is not None:
                    scenario.update(body=body, content_type="text/html")
                terminal = self.run_to_terminal(instance, "Task", "r1", scenario)
                expected = f"http_{status}" if body is None else f"http_{status}_unvalidated"
                self.assertEqual((terminal["state"], terminal["error_code"], terminal["availability"]), ("failed_after_send", expected, "provider_unavailable"), terminal)
                self.assertTrue(terminal["possibly_billed"])
                self.assertEqual(terminal["remote_outcome"], "unknown")
                self.assertTrue(terminal["stops_room_lane"])
                if body is None:
                    self.assertEqual(terminal["error_type"], "server_error")
                with self.assertRaises(adapter.AdapterError) as refused:
                    instance.submit("Task", "r2", diagnosis="a diagnosis cannot reopen an ambiguous 5xx outcome")
                self.assertEqual(refused.exception.code, "room_stopped")
                self.assertNotIn("PROVIDER_PROSE_MUST_NOT_LEAK", json.dumps(terminal))

    def test_validated_provider_rejections_are_definite_and_need_a_diagnosis_to_retry(self):
        expectations = {401: "credential_rejected", 400: "request_rejected", 402: "insufficient_balance", 404: "model_unavailable",
                        429: "provider_unavailable", 422: "request_rejected"}
        for status, availability in expectations.items():
            with self.subTest(status=status):
                instance = self.room(f"reject-{status}")
                terminal = self.run_to_terminal(instance, "Task", "r1", {"kind": "http_error", "status": status, "type": "invalid_request_error", "code": "fixture_code"})
                self.assertEqual((terminal["state"], terminal["error_code"], terminal["availability"]), ("rejected_before_generation", f"http_{status}", availability))
                self.assertEqual(terminal["error_type"], "invalid_request_error")
                self.assertFalse(terminal["possibly_billed"])
                self.assertEqual(terminal["remote_outcome"], "rejected")
                self.assertFalse(terminal["stops_room_lane"])
                self.assertNotIn("PROVIDER_PROSE_MUST_NOT_LEAK", json.dumps(terminal))
        instance = self.room("reject-retry")
        rejected = self.run_to_terminal(instance, "Retry me", "r1", {"kind": "http_error", "status": 401})
        with self.assertRaises(adapter.AdapterError) as refused:
            instance.submit("Retry me", "r2")
        self.assertEqual(refused.exception.code, "diagnosis_required")
        self.assertEqual(refused.exception.extra["existing_job_id"], rejected["job_id"])
        requests_before = len(self.fake.requests)
        self.fake.scenario = {"kind": "ok"}
        retried = instance.submit("Retry me", "r2", diagnosis="The key file had been rotated; the saved key is now the active one.")
        self.assertEqual(self.wait(retried["job_id"], instance=instance)["state"], "completed")
        self.assertEqual(len(self.fake.requests), requests_before + 1)
        with self.assertRaises(adapter.AdapterError) as duplicate:
            instance.submit("Retry me", "r3", diagnosis="again")
        self.assertEqual(duplicate.exception.code, "duplicate_payload")

    def test_completed_requires_the_exact_observed_model_in_every_path(self):
        absent = self.run_to_terminal(self.room("model-absent"), "Who made you?", "r1", {"kind": "ok", "model": None})
        self.assertEqual((absent["state"], absent["error_code"], absent["observed_model"], absent["finish_reason"]),
                         ("failed_after_send", "model_unverified", None, "stop"))
        self.assertFalse(absent["complete"])
        self.assertTrue(absent["possibly_billed"])
        with self.assertRaises(adapter.AdapterError) as refused:
            self.room("model-absent").result(absent["job_id"])
        self.assertEqual(refused.exception.code, "result_unavailable")
        self.assertEqual(list((self.home / "deepseek" / "exports" / "model-absent").iterdir()), [])
        self.fake.scenario = {"kind": "ok", "model": None, "answer": "OK"}
        receipt = adapter.run_probe(self.room("probe-model-absent"))
        self.assertEqual((receipt["state"], receipt["observed_model"], receipt["expected_answer_matched"], receipt["error_code"]),
                         ("failed_after_send", None, None, "model_unverified"))
        self.assertEqual(receipt["requested"]["model"], adapter.DEFAULT_MODEL, "the requested identity is reported separately, never as observed")
        asked = self.room("ask-model-absent").ask("Quick?", "a1", effort="none")
        self.assertEqual((asked["state"], asked["error_code"]), ("failed_after_send", "model_unverified"))
        self.assertNotIn("answer", asked)

    def test_provider_derived_fields_never_carry_the_key_or_raw_prose(self):
        secret = SYNTHETIC_KEY
        cases = [("envelope", {"kind": "http_error", "status": 401, "type": secret, "code": secret}, "rejected_before_generation", "http_401", "unrecognized"),
                 ("stream-error", {"kind": "ok", "error_in_stream": True, "error_type": secret}, "failed_after_send", "provider_error_in_stream", "unrecognized"),
                 ("model", {"kind": "ok", "model": secret}, "failed_after_send", "model_mismatch", None),
                 ("finish", {"kind": "ok", "finish": secret}, "truncated", "finish_other", None),
                 ("usage", {"kind": "ok", "usage_value": {secret: 1, "prompt_tokens": 3, secret + "_details": {secret: 2}}}, "completed", None, None)]
        for name, scenario, state, code, error_type in cases:
            with self.subTest(case=name):
                instance = self.room("leak-" + name)
                terminal = self.run_to_terminal(instance, "Task " + name, "r1", scenario)
                self.assertEqual((terminal["state"], terminal["error_code"], terminal["error_type"]), (state, code, error_type), terminal)
                if name == "finish":
                    self.assertEqual(terminal["finish_reason"], "other")
                if name == "usage":
                    self.assertEqual((terminal["usage"], terminal["usage_source"]), ({"prompt_tokens": 3}, "final_chunk"))
                if name == "model":
                    self.assertIsNone(terminal["observed_model"])
                job_dir = self.job_dir(terminal["job_id"])
                surfaces = {"status": json.dumps(terminal), "health": json.dumps(instance.health()), "ledger": json.dumps(instance.ledger.job(terminal["job_id"]))}
                for artifact in ("meta.json", "wire-tail", "worker.log", "heartbeat.json"):
                    if (job_dir / artifact).exists():
                        surfaces[artifact] = (job_dir / artifact).read_bytes().decode("utf-8", "replace")
                for surface, serialized in surfaces.items():
                    self.assertNotIn(secret, serialized, surface)
                    if surface != "wire-tail":  # the private raw tail is the one place provider bytes are kept, key-redacted
                        self.assertNotIn("PROVIDER_PROSE_MUST_NOT_LEAK", serialized, surface)
                self.assertIn("[REDACTED]", surfaces["wire-tail"], "private diagnostics are redacted, not trusted")
        instance = self.room("leak-usage-only")
        terminal = self.run_to_terminal(instance, "Only unknown counters", "r1", {"kind": "ok", "usage_value": {secret: 1}})
        self.assertEqual((terminal["state"], terminal["usage"], terminal["usage_source"]), ("completed", None, "unrecognized"), "unknown counters are not zero")
        self.fake.scenario = {"kind": "http_error", "status": 401, "type": secret, "code": secret}
        receipt = adapter.run_probe(self.room("leak-probe"))
        self.assertEqual((receipt["state"], receipt["error_type"], receipt["availability"]), ("rejected_before_generation", "unrecognized", "credential_rejected"))
        for saved in (self.home / "deepseek" / "probes").glob("*.json"):
            self.assertNotIn(secret, saved.read_text())

    def test_fixed_deadline_is_checked_before_and_after_connecting_and_never_extended(self):
        events, timeouts = [], []

        class Spy:
            sock = None

            def __init__(self, delay=0.0):
                self.delay = delay

            def connect(self):
                events.append("connect")
                time.sleep(self.delay)

            def request(self, *args, **kwargs):
                events.append("request")

            def getresponse(self):
                events.append("response")
                raise OSError("synthetic response failure")

            def close(self):
                events.append("close")

        def opener(delay):
            def open_connection(host, port, context, timeout):
                timeouts.append(timeout)
                return Spy(delay)
            return open_connection
        config = self.adapter.config
        fd = os.open(self.base, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with mock.patch.object(adapter, "_open_connection", opener(0.0)):
                expired = adapter.execute_request(config, SYNTHETIC_KEY, {}, fd, time.monotonic() - 1)
            self.assertEqual((expired["state"], expired["error_code"], expired["possibly_billed"], expired["remote_outcome"]),
                             ("not_started", "local_deadline_before_send", False, "not_sent"))
            self.assertEqual(events, [], "an elapsed deadline never connects")
            with mock.patch.object(adapter, "_open_connection", opener(0.3)):
                during = adapter.execute_request(config, SYNTHETIC_KEY, {}, fd, time.monotonic() + 0.1)
            self.assertEqual((during["state"], during["error_code"], during["possibly_billed"]), ("not_started", "local_deadline_before_send", False))
            self.assertEqual(events, ["connect", "close"], "a connect that outlasts the deadline is followed by no send")
            events.clear()
            with mock.patch.object(adapter, "_open_connection", opener(0.0)):
                cancelled = adapter.execute_request(config, SYNTHETIC_KEY, {}, fd, time.monotonic() + 5, should_cancel=lambda: True)
            self.assertEqual((cancelled["state"], cancelled["error_code"]), ("not_started", "cancelled_before_send"))
            self.assertEqual(events, [])
            timeouts.clear()
            with mock.patch.object(adapter, "_open_connection", opener(0.0)):
                sent = adapter.execute_request(config, SYNTHETIC_KEY, {}, fd, time.monotonic() + 0.5)
            self.assertEqual((sent["state"], sent["error_code"], sent["possibly_billed"]), ("unknown_delivery", "transport_response_failed", True))
            self.assertEqual(events, ["connect", "request", "response", "close"])
            self.assertTrue(0 < timeouts[0] <= 0.5, "the connect timeout is bounded by the remaining time, not only the idle window")
        finally:
            os.close(fd)
        for status in (408, 409):
            with self.subTest(status=status):
                instance = self.room(f"ambiguous-{status}")
                terminal = self.run_to_terminal(instance, "Task", "r1", {"kind": "http_error", "status": status, "type": "invalid_request_error"})
                self.assertEqual((terminal["state"], terminal["error_code"], terminal["availability"]), ("failed_after_send", f"http_{status}_ambiguous", "outcome_ambiguous"))
                self.assertTrue(terminal["possibly_billed"])
                self.assertTrue(terminal["stops_room_lane"])
                with self.assertRaises(adapter.AdapterError) as refused:
                    instance.submit("Task", "r2", diagnosis="a diagnosis cannot reopen an ambiguous outcome")
                self.assertEqual(refused.exception.code, "room_stopped")

    def test_worker_refuses_a_tampered_inventory_and_a_descriptor_of_another_job(self):
        room_root = self.base / "room-root"
        room_root.mkdir(mode=0o700)
        inventory = {"provider": "deepseek", "room_id": self.room_id, "export_dir": str(self.export_dir),
                     "files": {str(self.config_path): adapter.sha(self.config_path.read_bytes()), ADAPTER: "0" * 64}}
        (room_root / "settings.json").write_text(json.dumps({"provider_inventory": inventory}))
        tampered = adapter.Adapter(self.home, self.room_id, self.config_path, room_root=room_root)
        self.assertEqual(tampered.integrity["reason"], "adapter_source_mismatch")
        submitted = tampered.submit("Inventory mismatch reaches the worker", "w1")
        terminal = self.wait(submitted["job_id"], instance=tampered)
        self.assertEqual((terminal["state"], terminal["error_code"], terminal["possibly_billed"]), ("not_started", "provider_inventory_mismatch", False))
        self.assertEqual(self.fake.requests, [])
        other = self.room("other-lease")
        with mock.patch.object(self.adapter, "_spawn_worker", lambda job_id, job_fd, lease_fd: None), \
                mock.patch.object(other, "_spawn_worker", lambda job_id, job_fd, lease_fd: None):
            mine = self.submit("Held by the wrong descriptor", "d1")
            theirs = other.submit("Another job's lease", "d1")
        foreign = os.open(self.job_dir(theirs["job_id"]) / "worker.lock", os.O_RDWR | os.O_NOFOLLOW)
        try:
            fcntl.flock(foreign, fcntl.LOCK_EX | fcntl.LOCK_NB)
            process = subprocess.run([sys.executable, str(self.boot), str(self.fake.port), ADAPTER, "worker", "--home", str(self.home), "--room", self.room_id,
                                      "--config", str(self.config_path), "--job", mine["job_id"], "--lease-fd", str(foreign)],
                                     pass_fds=(foreign,), capture_output=True, timeout=30)
        finally:
            os.close(foreign)
        self.assertEqual(process.returncode, 2, process.stderr)
        self.assertEqual((self.adapter.ledger.job(mine["job_id"])["state"], other.ledger.job(theirs["job_id"])["state"]), ("queued", "queued"))
        self.assertEqual(self.fake.requests, [])
        with mock.patch.object(adapter, "STARTUP_GRACE_SECONDS", 0.0):
            self.assertEqual(self.adapter.status(mine["job_id"], True, 0.5)["state"], "not_started")

    def test_durable_request_accounting_holds_for_escape_heavy_input_at_the_bound(self):
        framing = len(adapter.FRAMING.encode("utf-8")) + len("TASK:\n".encode("utf-8"))
        budget = FAST["max_input_bytes"] - framing  # task bytes that exactly fill the framed-input allowance
        reservation = adapter.reservation_bytes(self.adapter.config)
        cases = {"nul": "X" + "\x00" * (budget - 1), "newlines": "\n" * (budget - 1) + "x", "backslashes": "\\" * budget,
                 "quotes": '"' * budget, "multibyte": "é" * (budget // 2), "mixed": ("\x00\n\\\"\t" * budget)[:budget]}
        with mock.patch.object(adapter.Adapter, "_spawn_worker", lambda self, job_id, job_fd, lease_fd: None):
            for name, task in cases.items():
                with self.subTest(case=name):
                    instance = self.room("acct-" + name)
                    submitted = instance.submit(task, "acct-1")
                    expected_text = "TASK:\n" + task
                    self.assertEqual(submitted["input_bytes"], len(adapter.FRAMING.encode("utf-8")) + len(expected_text.encode("utf-8")))
                    self.assertLessEqual(submitted["input_bytes"], FAST["max_input_bytes"])
                    job_dir = self.job_dir(submitted["job_id"])
                    self.assertEqual((job_dir / "request-text").stat().st_size, len(expected_text.encode("utf-8")), "stored bytes are the measured bytes")
                    self.assertLessEqual((job_dir / "request.json").stat().st_size, adapter.MAX_REQUEST_RECORD_BYTES)
                    self.assertNotIn(b"user_text\"", (job_dir / "request.json").read_bytes().replace(b"user_text_bytes", b"").replace(b"user_text_sha256", b""))
                    retained = sum(path.stat().st_size for path in job_dir.iterdir() if path.is_file())
                    self.assertLessEqual(retained, reservation, "every retained file fits the admission reservation")
                    self.assertEqual(instance.ledger.storage()["reserved_bytes"], reservation)
                    fd = instance._job_fd(submitted["job_id"])
                    try:
                        parameters, loaded = instance._load_request(fd)  # the worker's own loading path, no send
                    finally:
                        os.close(fd)
                    self.assertEqual(loaded, expected_text)
                    self.assertEqual(parameters["max_tokens"], FAST["max_tokens"])
                    with mock.patch.object(adapter, "STARTUP_GRACE_SECONDS", 0.0):
                        relabelled = instance.status(submitted["job_id"], True, 0.3)
                    self.assertEqual(relabelled["state"], "not_started")
                    self.assertLessEqual(instance.ledger.job(submitted["job_id"])["retained_bytes"], reservation)
            over = self.room("acct-over")
            with self.assertRaises(adapter.AdapterError) as too_large:
                over.submit("X" + "\x00" * budget, "acct-over")
            self.assertEqual(too_large.exception.code, "input_too_large")
            self.assertEqual(over.ledger.active("acct-over"), [])
            self.assertEqual(list((self.home / "deepseek" / "jobs").glob("*/request-text")).__len__(), len(cases), "a refused input leaves no job directory")
            with mock.patch.object(adapter, "MAX_REQUEST_RECORD_BYTES", 512), self.assertRaises(adapter.AdapterError) as metadata:
                self.room("acct-meta").submit("A small task", "acct-meta")
            self.assertEqual(metadata.exception.code, "request_metadata_too_large")
            with self.assertRaises(adapter.AdapterError) as diagnosis:
                self.room("acct-diag").submit("A task", "acct-diag", diagnosis="d" * (adapter.MAX_DIAGNOSIS_BYTES + 1))
            self.assertEqual(diagnosis.exception.code, "diagnosis_invalid")
            tampered_room = self.room("acct-tamper")
            tampered = tampered_room.submit("Original text", "acct-tamper")
        job_dir = self.job_dir(tampered["job_id"])
        (job_dir / "request-text").write_bytes(b"TASK:\nReplaced text")
        lease = os.open(job_dir / "worker.lock", os.O_RDWR | os.O_NOFOLLOW)
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            process = subprocess.run([sys.executable, str(self.boot), str(self.fake.port), ADAPTER, "worker", "--home", str(self.home), "--room", "acct-tamper",
                                      "--config", str(self.config_path), "--job", tampered["job_id"], "--lease-fd", str(lease)],
                                     pass_fds=(lease,), capture_output=True, timeout=30)
        finally:
            os.close(lease)
        self.assertEqual(process.returncode, 0, process.stderr)
        terminal = tampered_room.status(tampered["job_id"], True, 1)
        self.assertEqual((terminal["state"], terminal["error_code"], terminal["possibly_billed"]), ("not_started", "request_unreadable", False))
        self.assertEqual(self.fake.requests, [], "a record that does not match its text is never sent")

    def test_cancel_and_local_deadline_abandon_without_stopping_distinct_tasks(self):
        instance = self.room("abandon")
        self.fake.scenario = {"kind": "ok", "slow": 0.4}
        submitted = instance.submit("Slow task", "r1")
        started = time.monotonic()
        while instance.status(submitted["job_id"], True, 0.5)["state"] != "streaming" and time.monotonic() - started < 10:
            pass
        cancelled = instance.cancel(submitted["job_id"])
        self.assertTrue(cancelled["cancel_requested"])
        self.assertEqual(cancelled["remote_outcome"], "unknown")
        terminal = self.wait(submitted["job_id"], instance=instance)
        self.assertEqual((terminal["state"], terminal["error_code"]), ("abandoned_cancelled", "cancelled"))
        self.assertTrue(terminal["possibly_billed"])
        self.assertFalse(terminal["stops_room_lane"])
        self.assertFalse(instance.cancel(submitted["job_id"])["cancel_requested"])
        with self.assertRaises(adapter.AdapterError) as duplicate:
            instance.submit("Slow task", "r2")
        self.assertEqual(duplicate.exception.code, "duplicate_payload")
        self.fake.scenario = {"kind": "ok", "slow": 1.5}
        expired = instance.submit("Another slow task", "r3")
        terminal = self.wait(expired["job_id"], instance=instance)
        self.assertEqual((terminal["state"], terminal["error_code"]), ("abandoned_local_deadline", "local_deadline"), terminal)
        self.assertFalse(terminal["stops_room_lane"])
        self.fake.scenario = {"kind": "ok"}
        fresh = instance.submit("A third distinct task", "r4")
        self.assertEqual(self.wait(fresh["job_id"], instance=instance)["state"], "completed")

    def test_idle_timeout_and_silent_provider_are_unknown_or_failed_after_send(self):
        self.write_config({**FAST, "request_timeout_seconds": 8, "read_idle_seconds": 2})
        hung = self.room("hang")
        terminal = self.run_to_terminal(hung, "Hang", "r1", {"kind": "hang", "seconds": 4})
        self.assertEqual((terminal["state"], terminal["error_code"]), ("unknown_delivery", "transport_response_failed"))
        self.assertTrue(terminal["stops_room_lane"])
        idle = self.room("idle")
        terminal = self.run_to_terminal(idle, "Idle", "r1", {"kind": "ok", "slow": 3.5})
        self.assertEqual((terminal["state"], terminal["error_code"]), ("failed_after_send", "read_idle_timeout"))

    def test_key_ancestors_are_walked_without_following_symlinks_or_trusting_writable_directories(self):
        self.assertTrue(adapter.key_diagnostics(self.key_path)["ok"])
        self.home.chmod(0o777)
        self.assertEqual(adapter.key_diagnostics(self.key_path)["reason"], "key_unsafe_ancestor")
        with self.assertRaises(adapter.AdapterError) as writable:
            adapter.read_api_key(self.key_path)
        self.assertEqual(writable.exception.code, "key_unsafe_ancestor")
        self.home.chmod(0o1777)
        self.assertTrue(adapter.key_diagnostics(self.key_path)["ok"], "a sticky world-writable ancestor such as /tmp is tolerated")
        self.home.chmod(0o775)
        self.assertEqual(adapter.key_diagnostics(self.key_path)["reason"], "key_unsafe_ancestor")
        self.home.chmod(0o755)
        self.assertTrue(adapter.key_diagnostics(self.key_path)["ok"])
        self.home.chmod(0o700)
        link = self.base / "linked-home"
        link.symlink_to(self.home)
        self.assertEqual(adapter.key_diagnostics(link / "secrets" / "deepseek-api-key")["reason"], "key_unsafe_ancestor")
        with self.assertRaises(adapter.AdapterError) as symlinked:
            adapter.read_api_key(link / "secrets" / "deepseek-api-key")
        self.assertEqual(symlinked.exception.code, "key_unsafe_ancestor")
        self.secrets.chmod(0o710)
        self.assertEqual(adapter.key_diagnostics(self.key_path)["reason"], "key_unsafe_ancestor")
        self.secrets.chmod(0o700)
        for bad in ("relative/deepseek-api-key", "/", "/private/../deepseek-api-key"):
            with self.subTest(bad=bad):
                self.assertEqual(adapter.key_diagnostics(bad)["reason"], "key_unsafe_ancestor")
        self.write_config({**FAST, "api_key_file": str(link / "secrets" / "deepseek-api-key")})
        linked_room = self.room("key-link")
        terminal = self.run_to_terminal(linked_room, "Task", "r1", {"kind": "ok"})
        self.assertEqual((terminal["state"], terminal["error_code"]), ("not_started", "key_unsafe_ancestor"))
        self.assertEqual(self.fake.requests, [])

    def test_credential_problems_are_proven_unsent_and_environment_keys_are_ignored(self):
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": SYNTHETIC_KEY}):
            self.key_path.chmod(0o644)
            unsafe = self.run_to_terminal(self.room("key-mode"), "Task", "r1", {"kind": "ok"})
            self.assertEqual((unsafe["state"], unsafe["error_code"], unsafe["possibly_billed"]), ("not_started", "key_mode", False))
            self.key_path.unlink()
            missing = self.run_to_terminal(self.room("key-missing"), "Task", "r1", {"kind": "ok"})
            self.assertEqual((missing["state"], missing["error_code"]), ("not_started", "key_missing"))
        self.assertEqual(self.fake.requests, [])
        self.key_path.write_text(SYNTHETIC_KEY + "\n")
        self.key_path.chmod(0o600)
        leaked = self.run_to_terminal(self.room("key-payload"), "Use " + SYNTHETIC_KEY + " here", "r1", {"kind": "ok"})
        self.assertEqual((leaked["state"], leaked["error_code"]), ("not_started", "credential_in_payload"))
        self.assertEqual(self.fake.requests, [])
        self.assertNotIn(SYNTHETIC_KEY, json.dumps(leaked))
        again = self.room("key-payload").submit("Use " + SYNTHETIC_KEY + " here", "r2")
        self.assertFalse(again["duplicate"], "not_started permits a new request_id")
        self.assertEqual(self.wait(again["job_id"], instance=self.room("key-payload"))["state"], "not_started")

    def test_input_bounds_and_context_path_safety(self):
        with self.assertRaises(adapter.AdapterError) as too_large:
            self.submit("x" * 3000, "big")
        self.assertEqual(too_large.exception.code, "input_too_large")
        self.assertIn("provisional_input_tokens", too_large.exception.extra["estimate"])
        self.assertEqual(self.adapter.ledger.storage()["jobs"], 0)
        good = str(self.worktree / "module.py")
        (self.worktree / ".env").write_text("SECRET=1")
        (self.worktree / "server.key").write_text("private")
        (self.worktree / "notes.bin").write_bytes(b"\x00binary")
        (self.worktree / "link.py").symlink_to(self.worktree / "module.py")
        (self.worktree / "large.py").write_text("#" * 2000)
        outside = self.base / "outside.py"
        outside.write_text("x = 1")
        cases = [("outside", str(outside), "context_path_outside_worktree"), ("dotfile", str(self.worktree / ".env"), "context_path_invalid"),
                 ("credential", str(self.worktree / "server.key"), "context_path_credential"),
                 ("suffix", str(self.worktree / "notes.bin"), "context_path_suffix"), ("symlink", str(self.worktree / "link.py"), "context_unsafe"),
                 ("oversized", str(self.worktree / "large.py"), "context_oversized"), ("traversal", str(self.worktree / "sub" / ".." / "module.py"), "context_path_invalid"),
                 ("relative", "module.py", "context_path_invalid"), ("missing", str(self.worktree / "absent.py"), "context_missing"),
                 ("too-many", [good] * 17, "context_files_limit"), ("duplicate", [good, good], "context_path_invalid")]
        for name, value, code in cases:
            with self.subTest(case=name), self.assertRaises(adapter.AdapterError) as refused:
                self.submit("Read it", "ctx-" + name, context_path=value)
            self.assertEqual(refused.exception.code, code, name)
        with mock.patch.dict(os.environ, {"PROJECT_ROOM_WORKTREE": ""}), self.assertRaises(adapter.AdapterError) as absent:
            self.submit("Read it", "ctx-noroot", context_path=good)
        self.assertEqual(absent.exception.code, "worktree_root_unavailable")
        self.assertEqual(self.adapter.ledger.storage()["jobs"], 0)
        first = self.submit("Read it", "ctx-good", context_path=good)
        self.assertEqual(self.wait(first["job_id"])["state"], "completed")
        prompt = self.fake.requests[-1]["body"]["messages"][1]["content"]
        self.assertIn("FILE module.py", prompt)
        self.assertIn("def value():", prompt)
        (self.worktree / "module.py").write_text("def value():\n    return 2\n")
        second = self.submit("Read it", "ctx-changed", context_path=good)
        self.assertNotEqual(second["job_id"], first["job_id"], "changed file bytes are a different payload")
        self.wait(second["job_id"])

    def test_ask_lane_uses_small_budget_and_optional_thinking(self):
        self.fake.scenario = {"kind": "ok", "answer": "Forty-two"}
        answer = self.adapter.ask("Quick question?", "ask-1", effort="none")
        self.assertEqual((answer["state"], answer["lane"], answer["answer"]), ("completed", "ask", "Hello, wörld Forty-two"))
        body = self.fake.requests[-1]["body"]
        self.assertEqual((body["thinking"], body["max_tokens"]), ({"type": "disabled"}, FAST["ask_max_tokens"]))
        self.assertNotIn("reasoning_effort", body)
        low = self.adapter.ask("Another question?", "ask-2")
        self.assertEqual(low["state"], "completed")
        body = self.fake.requests[-1]["body"]
        self.assertEqual((body["thinking"], body["reasoning_effort"]), ({"type": "enabled"}, "low"))
        with self.assertRaises(adapter.AdapterError) as refused:
            self.adapter.ask("Third?", "ask-3", effort="max")
        self.assertEqual(refused.exception.code, "effort_invalid")

    def test_admission_counts_both_lanes_per_room_and_across_the_host(self):
        self.fake.scenario = {"kind": "ok", "slow": 0.3}
        first = self.submit("Busy one", "b1")
        with self.assertRaises(adapter.AdapterError) as room_busy:
            self.adapter.ask("Quick while busy", "b2")
        self.assertEqual((room_busy.exception.code, room_busy.exception.extra["scope"]), ("busy", "room"))
        other = self.room("second-room")
        second = other.submit("Busy two", "b1")
        third_room = self.room("third-room")
        with self.assertRaises(adapter.AdapterError) as host_busy:
            third_room.submit("Busy three", "b1")
        self.assertEqual((host_busy.exception.code, host_busy.exception.extra["scope"]), ("busy", "host"))
        for instance, job in ((self.adapter, first), (other, second)):
            self.assertEqual(self.wait(job["job_id"], instance=instance)["state"], "completed")
        self.fake.scenario = {"kind": "ok"}
        self.assertEqual(self.wait(third_room.submit("Busy three", "b1")["job_id"], instance=third_room)["state"], "completed")

    def test_storage_and_job_quotas_refuse_before_any_request(self):
        self.write_config({**FAST, "max_retained_bytes": adapter.reservation_bytes({**adapter.DEFAULTS, **FAST}) + 10, "max_jobs": 2})
        quota = self.room("quota")
        first = quota.submit("One", "q1")
        self.assertEqual(self.wait(first["job_id"], instance=quota)["state"], "completed")
        with self.assertRaises(adapter.AdapterError) as storage:
            quota.submit("Two", "q2")
        self.assertEqual(storage.exception.code, "storage_exhausted")
        self.write_config({**FAST, "max_jobs": 1})
        limited = adapter.Adapter(self.home, "quota", self.config_path)
        with self.assertRaises(adapter.AdapterError) as count:
            limited.submit("Two", "q2")
        self.assertEqual(count.exception.code, "job_limit")
        self.assertEqual(len(self.fake.requests), 1)

    def test_rooms_never_share_jobs_results_or_deduplication(self):
        mine = self.run_to_terminal(self.adapter, "Shared task text", "r1", {"kind": "ok"})
        other = self.room("other-room")
        for operation in (lambda: other.status(mine["job_id"], True, 1), lambda: other.result(mine["job_id"]), lambda: other.cancel(mine["job_id"])):
            with self.assertRaises(adapter.AdapterError) as refused:
                operation()
            self.assertEqual(refused.exception.code, "job_unknown")
        theirs = other.submit("Shared task text", "r1")
        self.assertNotEqual(theirs["job_id"], mine["job_id"])
        self.assertEqual(self.wait(theirs["job_id"], instance=other)["state"], "completed")
        self.assertEqual(len(self.fake.requests), 2)
        self.assertEqual({row["room_id"] for row in [self.adapter.ledger.job(mine["job_id"]), other.ledger.job(theirs["job_id"])]}, {"fixture-room", "other-room"})

    def test_vanished_worker_is_unknown_delivery_until_the_user_resolves_at_a_tty(self):
        self.fake.scenario = {"kind": "ok", "slow": 0.5}
        submitted = self.submit("Vanishing task", "v1")
        deadline = time.monotonic() + 10
        while self.adapter.status(submitted["job_id"], True, 0.5)["state"] != "streaming" and time.monotonic() < deadline:
            pass
        pid = self.adapter.ledger.job(submitted["job_id"])["worker_pid"]
        os.kill(pid, 9)
        with mock.patch.object(adapter, "STARTUP_GRACE_SECONDS", 0.2):
            terminal = self.wait(submitted["job_id"])
        self.assertEqual((terminal["state"], terminal["error_code"]), ("unknown_delivery", "worker_vanished"))
        self.assertTrue(terminal["stops_room_lane"])
        with self.assertRaises(adapter.AdapterError) as stopped:
            self.submit("Distinct task", "v2")
        self.assertEqual(stopped.exception.code, "room_stopped")
        self.assertEqual(stopped.exception.extra["job_id"], submitted["job_id"])
        health = self.adapter.health()
        self.assertTrue(health["room_stop"]["stopped"])
        self.assertIn(submitted["job_id"], health["room_stop"]["jobs"][0]["resolve_command"])
        note = "User decision: the remote outcome is unknown and may have been billed; proceed with new distinct work."
        with self.assertRaises(adapter.AdapterError) as tty:
            adapter.resolve_job(self.home, self.room_id, submitted["job_id"], note, interactive=False)
        self.assertEqual(tty.exception.code, "tty_required")
        with self.assertRaises(adapter.AdapterError) as wrong_room:
            adapter.resolve_job(self.home, "other-room", submitted["job_id"], note, interactive=True)
        self.assertEqual(wrong_room.exception.code, "job_unknown")
        with self.assertRaises(adapter.AdapterError) as empty:
            adapter.resolve_job(self.home, self.room_id, submitted["job_id"], "  ", interactive=True)
        self.assertEqual(empty.exception.code, "note_required")
        completed = self.run_to_terminal(self.room("eligibility"), "Fine", "r1", {"kind": "ok"})
        with self.assertRaises(adapter.AdapterError) as ineligible:
            adapter.resolve_job(self.home, "eligibility", completed["job_id"], note, interactive=True)
        self.assertEqual(ineligible.exception.code, "resolve_not_eligible")
        requests = len(self.fake.requests)
        resolved = adapter.resolve_job(self.home, self.room_id, submitted["job_id"], note, interactive=True)
        self.assertEqual((resolved["resolved"], resolved["duplicate"], resolved["state"], resolved["resent"]), (True, False, "unknown_delivery", False))
        again = adapter.resolve_job(self.home, self.room_id, submitted["job_id"], "a different note", interactive=True)
        self.assertEqual((again["duplicate"], again["note_sha256"]), (True, resolved["note_sha256"]))
        projection = self.job_dir(submitted["job_id"]) / "resolution.json"
        self.assertEqual(json.loads(projection.read_text())["note"], note)
        self.assertEqual(len(self.fake.requests), requests, "resolve sends nothing")
        self.assertEqual(self.adapter.status(submitted["job_id"], True, 1)["state"], "unknown_delivery")
        self.assertFalse(self.adapter.status(submitted["job_id"], True, 1)["stops_room_lane"])
        projection.unlink()
        self.adapter.status(submitted["job_id"], True, 1)
        self.assertTrue(projection.exists(), "an interrupted projection is reconciled from the durable row")
        with self.assertRaises(adapter.AdapterError) as duplicate:
            self.submit("Vanishing task", "v3")
        self.assertEqual(duplicate.exception.code, "duplicate_payload")
        self.fake.scenario = {"kind": "ok"}
        fresh = self.submit("Distinct task", "v2")
        self.assertEqual(self.wait(fresh["job_id"])["state"], "completed")

    def test_unlaunched_jobs_become_not_started_only_after_the_grace_and_only_when_lease_free(self):
        with mock.patch.object(self.adapter, "_spawn_worker", lambda job_id, job_fd, lease_fd: None):
            submitted = self.submit("Never launched", "n1")
        with mock.patch.object(adapter.fcntl, "flock", side_effect=AssertionError("no lease probe inside the grace")):
            self.assertEqual(self.adapter.status(submitted["job_id"], True, 0.3)["state"], "queued")
        with mock.patch.object(adapter, "STARTUP_GRACE_SECONDS", 0.0):
            job_dir = self.job_dir(submitted["job_id"])
            with (job_dir / "worker.lock").open("a") as held:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertEqual(self.adapter.status(submitted["job_id"], True, 0.3)["state"], "queued", "a held lease is never relabelled")
            terminal = self.adapter.status(submitted["job_id"], True, 1)
        self.assertEqual((terminal["state"], terminal["error_code"], terminal["possibly_billed"]), ("not_started", "not_launched", False))
        self.assertEqual(self.fake.requests, [])
        again = self.submit("Never launched", "n2")
        self.assertFalse(again["duplicate"])
        self.assertEqual(self.wait(again["job_id"])["state"], "completed")
        with mock.patch.object(adapter.subprocess, "Popen", side_effect=OSError("fixture spawn failure")):
            with self.assertRaises(adapter.AdapterError) as failed:
                self.submit("Spawn failure", "n3")
        self.assertEqual(failed.exception.code, "spawn_failed")
        self.assertEqual(self.adapter.status(failed.exception.extra["job_id"], True, 0.2)["state"], "not_started")

    def test_invalid_inherited_descriptor_never_runs_and_never_sends(self):
        with mock.patch.object(self.adapter, "_spawn_worker", lambda job_id, job_fd, lease_fd: None):
            submitted = self.submit("Bogus lease", "l1")
        bogus = os.open(os.devnull, os.O_RDWR)
        try:
            process = subprocess.run([sys.executable, str(self.boot), str(self.fake.port), ADAPTER, "worker", "--home", str(self.home), "--room", self.room_id,
                                      "--config", str(self.config_path), "--job", submitted["job_id"], "--lease-fd", str(bogus)],
                                     pass_fds=(bogus,), capture_output=True, timeout=30)
        finally:
            os.close(bogus)
        self.assertEqual(process.returncode, 2, process.stderr)
        self.assertEqual(self.adapter.ledger.job(submitted["job_id"])["state"], "queued")
        self.assertEqual(self.fake.requests, [])
        with mock.patch.object(adapter, "STARTUP_GRACE_SECONDS", 0.0):
            self.assertEqual(self.adapter.status(submitted["job_id"], True, 0.5)["state"], "not_started")

    def test_concurrent_status_polls_during_launch_never_relabel_a_live_worker(self):
        self.fake.scenario = {"kind": "ok", "slow": 0.2}
        seen, stop = [], threading.Event()
        submitted = self.submit("Polled task", "p1")

        def poll():
            while not stop.is_set():
                seen.append(self.adapter.status(submitted["job_id"], True, 0.2)["state"])
        threads = [threading.Thread(target=poll) for _ in range(4)]
        with mock.patch.object(adapter, "STARTUP_GRACE_SECONDS", 0.0):
            for thread in threads:
                thread.start()
            terminal = self.wait(submitted["job_id"])
            stop.set()
            for thread in threads:
                thread.join(timeout=5)
        self.assertEqual(terminal["state"], "completed")
        self.assertNotIn("unknown_delivery", seen)
        self.assertNotIn("not_started", seen)
        self.assertEqual(len(self.fake.requests), 1)


class ConfigValidatorTests(unittest.TestCase):
    def test_defaults_are_the_pinned_deep_settings_and_derivations_hold(self):
        config = adapter.validate_config({}, "/private/home")
        self.assertEqual((config["model"], config["reasoning_effort"], config["max_tokens"], config["context_tokens"]),
                         (adapter.DEFAULT_MODEL, "max", 393216, 1048576))
        self.assertEqual(config["api_key_file"], "/private/home/secrets/deepseek-api-key")
        self.assertEqual((config["request_timeout_seconds"], config["max_wire_bytes"], config["max_input_bytes"]), (9891, 134217728, 614400))
        self.assertEqual(adapter.reservation_bytes(config), 614400 + 8388608 + 8388608 + 1048576)
        self.assertGreaterEqual(config["max_wire_bytes"], 393216 * 320 + 1048576)
        self.assertEqual(9891, -(-393216 // 40) + 60)

    def test_inconsistent_or_unsafe_configurations_are_refused(self):
        cases = [("old wire cap", {"max_wire_bytes": 33554432}), ("old timeout", {"request_timeout_seconds": 3600}),
                 ("content cap", {"max_content_bytes": 393216 * 8 - 1}), ("reasoning cap", {"max_reasoning_bytes": 1}),
                 ("input beyond window", {"max_input_bytes": 1048576 - 393216 + 1}), ("unknown field", {"stream_options": True}),
                 ("credential", {"api_key": "sk-x"}), ("base url", {"base_url": "https://api.deepseek.com/v1"}),
                 ("loopback url", {"base_url": "http://127.0.0.1:8080"}), ("model", {"model": "deepseek v4"}),
                 ("effort", {"reasoning_effort": "xhigh"}), ("ask effort", {"ask_effort": "high"}), ("bool", {"max_jobs": True}),
                 ("float", {"max_tokens": 393216.0}), ("relative key", {"api_key_file": "secrets/key"}),
                 ("host below room", {"max_concurrent_per_host": 1, "max_concurrent_per_room": 2}),
                 ("tail beyond metadata", {"max_wire_tail_bytes": 2 * 1048576}), ("idle beyond timeout", {"read_idle_seconds": 9891}),
                 ("tail leaves no metadata reserve", {"max_wire_tail_bytes": 1048576 - adapter.METADATA_RESERVE_BYTES + 1}),
                 ("quota below reservation", {"max_retained_bytes": 1})]
        for name, overrides in cases:
            with self.subTest(case=name), self.assertRaises(adapter.AdapterError) as refused:
                adapter.validate_config(overrides, "/private/home")
            self.assertEqual(refused.exception.code, "config_invalid", name)
        raised = adapter.validate_config({"max_wire_bytes": 393216 * 320 + 1048576}, "/private/home")
        self.assertEqual(raised["max_wire_bytes"], 126877696)
        self.assertEqual(adapter.worker_log_limit(adapter.validate_config({}, "/private/home")), 1048576 - 65536 - 65536)
        tight = adapter.validate_config({"max_wire_tail_bytes": 1048576 - adapter.METADATA_RESERVE_BYTES}, "/private/home")
        self.assertEqual(adapter.worker_log_limit(tight), 0, "the reserve is exact; the log bound can reach zero but never go negative")
        with self.assertRaises(adapter.AdapterError):
            adapter.validate_config({"max_wire_bytes": 393216 * 320 + 1048576 - 1}, "/private/home")


class ProductionBudgetStreamTests(AdapterFixture):
    config_overrides = {}

    def test_maximal_single_token_stream_completes_below_the_pinned_caps(self):
        tokens = adapter.DEFAULTS["max_tokens"]
        self.fake.scenario = {"kind": "big", "tokens": tokens}
        submitted = self.submit("Produce the longest answer", "big-1")
        terminal = self.wait(submitted["job_id"], seconds=240)
        self.assertEqual((terminal["state"], terminal["finish_reason"]), ("completed", "stop"), terminal)
        self.assertEqual(terminal["content_bytes"], tokens - tokens // 2)
        self.assertEqual(terminal["reasoning_bytes"], tokens // 2)
        self.assertGreater(terminal["wire_bytes"], tokens * 250)
        self.assertLess(terminal["wire_bytes"], adapter.DEFAULTS["max_wire_bytes"])
        self.assertEqual(terminal["usage"]["completion_tokens"], tokens)
        self.assertEqual(terminal["max_tokens"], tokens)
        self.assertEqual(self.fake.requests[0]["body"]["max_tokens"], tokens)
        result = self.adapter.result(submitted["job_id"], 0, 32768)
        self.assertEqual((len(result["text"]), result["next_offset"], result["total_chars"]), (32768, 32768, tokens - tokens // 2))
        tail = (self.job_dir(submitted["job_id"]) / "wire-tail").stat().st_size
        self.assertLessEqual(tail, adapter.DEFAULTS["max_wire_tail_bytes"])
        self.assertLessEqual(self.adapter.ledger.job(submitted["job_id"])["retained_bytes"], adapter.reservation_bytes(self.adapter.config))


class ArtifactAndHealthTests(AdapterFixture):
    def test_probe_writes_a_credential_free_private_receipt_visible_to_health(self):
        self.fake.scenario = {"kind": "ok", "answer": "OK"}
        receipt = adapter.run_probe(self.adapter)
        self.assertEqual((receipt["state"], receipt["expected_answer_matched"], receipt["observed_model"]), ("completed", False, adapter.DEFAULT_MODEL))
        self.fake.scenario = {"kind": "ok"}
        self.fake.scenario = {"kind": "ok", "answer": "OK"}
        body = self.fake.requests[-1]["body"]
        self.assertEqual((body["reasoning_effort"], body["max_tokens"], body["thinking"]), ("max", FAST["max_tokens"], {"type": "enabled"}))
        self.assertEqual(receipt["usage"]["total_tokens"], 61)
        self.assertEqual(receipt["usage_source"], "final_chunk")
        receipts = sorted((self.home / "deepseek" / "probes").glob("*.json"))
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0].stat().st_mode & 0o777, 0o600)
        serialized = receipts[0].read_text()
        self.assertNotIn(SYNTHETIC_KEY, serialized)
        self.assertNotIn("PRIVATE_REASONING_MUST_NOT_LEAK", serialized)
        latest = self.adapter.health()["latest_probe"]
        self.assertEqual((latest["receipt"], latest["state"], latest["http_status"]), (receipts[0].name, "completed", 200))
        self.assertNotIn("artifacts", latest)

    def test_health_reports_metadata_only_diagnostics(self):
        health = self.adapter.health()
        self.assertEqual((health["provider"], health["network"], health["model"]), ("deepseek", False, adapter.DEFAULT_MODEL))
        self.assertEqual(health["deep_lane"], {"thinking": "enabled", "reasoning_effort": "max", "max_tokens": FAST["max_tokens"]})
        self.assertTrue(health["key_file"]["ok"])
        self.assertEqual(health["integrity"]["reason"], "inventory_unavailable")
        self.assertTrue(health["export_dir"]["ok"])
        self.assertIn("unknown_delivery", health["state_table"])
        self.assertIsNone(health["latest_probe"])
        self.assertEqual(health["active"], {"room": 0, "host": 0, "max_room": 1, "max_host": 2, "meaning": health["active"]["meaning"]})
        self.key_path.chmod(0o640)
        self.assertEqual(self.adapter.health()["key_file"]["reason"], "key_unsafe_mode")
        self.key_path.unlink()
        self.assertEqual(self.adapter.health()["key_file"]["reason"], "key_missing")
        self.secrets.chmod(0o750)
        self.assertEqual(self.adapter.health()["key_file"]["reason"], "key_unsafe_ancestor")
        self.secrets.chmod(0o700)
        self.export_dir.rmdir()
        self.assertEqual(self.adapter.health()["export_dir"]["reason"], "export_dir_missing")
        self.assertNotIn(SYNTHETIC_KEY, json.dumps(health))

    def test_export_publication_fails_safely_and_never_overwrites(self):
        self.export_dir.rmdir()
        missing = self.run_to_terminal(self.adapter, "No export dir", "e1", {"kind": "ok"})
        self.assertEqual((missing["state"], missing["content_path"], missing["export_reason"]), ("completed", None, "export_dir_missing"))
        result = self.adapter.result(missing["job_id"])
        self.assertEqual(result["text"], "Hello, wörld OK")
        self.assertTrue((self.job_dir(missing["job_id"]) / "content").exists(), "the single authoritative copy stays private")
        self.export_dir.mkdir(mode=0o700)
        self.fake.scenario = {"kind": "ok", "slow": 0.3}
        conflict = self.submit("Conflicting export", "e2")
        planted = self.export_dir / (conflict["job_id"] + ".md")
        planted.write_text("planted different artifact")
        terminal = self.wait(conflict["job_id"])
        self.assertEqual((terminal["state"], terminal["content_path"], terminal["export_reason"]), ("completed", None, "export_conflict"))
        self.assertEqual(planted.read_text(), "planted different artifact")
        self.assertEqual(self.adapter.result(conflict["job_id"])["text"], "Hello, wörld OK")
        victim = self.base / "victim.txt"
        victim.write_text("must not change")
        linked = self.submit("Symlinked export", "e3")
        (self.export_dir / (linked["job_id"] + ".md")).symlink_to(victim)
        terminal = self.wait(linked["job_id"])
        self.assertEqual((terminal["state"], terminal["content_path"], terminal["export_reason"]), ("completed", None, "export_conflict"))
        self.assertEqual(victim.read_text(), "must not change")
        self.assertEqual(self.adapter.result(linked["job_id"])["text"], "Hello, wörld OK")

    def test_publication_failures_keep_one_retrievable_copy_at_every_boundary(self):
        content = "synthetic completed answer\n".encode("utf-8")
        digest = adapter.sha(content)
        export_ino = os.stat(self.export_dir).st_ino
        real = {name: getattr(os, name) for name in ("link", "fchmod", "unlink", "fsync")}

        def failing(message):
            def fail(*args, **kwargs):
                raise OSError(5, message)
            return fail

        def make_job():
            job_id = uuid.uuid4().hex
            fd = self.adapter._job_fd(job_id, create=True)
            try:
                adapter.write_below(fd, "content", content)
            finally:
                os.close(fd)
            return job_id

        def publish(job_id):
            fd = self.adapter._job_fd(job_id)
            try:
                return self.adapter._publish(fd, job_id, digest)
            finally:
                os.close(fd)

        def fsync_only(inode_matches, message):
            def fsync(fd):
                info = os.fstat(fd)
                if stat.S_ISDIR(info.st_mode) and inode_matches(info.st_ino):
                    raise OSError(5, message)
                return real["fsync"](fd)
            return fsync

        def unlink_content(name, *args, **kwargs):
            if name == "content":
                raise OSError(5, "synthetic unlink failure")
            return real["unlink"](name, *args, **kwargs)
        faults = {"link": ("link", failing("synthetic link failure")), "chmod": ("fchmod", failing("synthetic chmod failure")),
                  "fsync-room": ("fsync", fsync_only(lambda ino: ino == export_ino, "synthetic export directory fsync failure")),
                  "unlink": ("unlink", unlink_content),
                  "fsync-job": ("fsync", fsync_only(lambda ino: ino != export_ino, "synthetic job directory fsync failure"))}
        for name, (target, fault) in faults.items():
            with self.subTest(fault=name):
                job_id = make_job()
                with mock.patch.object(adapter.os, target, fault):
                    path, reason = publish(job_id)
                private, export = self.job_dir(job_id) / "content", self.export_dir / (job_id + ".md")
                if name in ("link", "chmod", "fsync-room"):
                    self.assertEqual((path, reason), (None, "export_unavailable"))
                    self.assertTrue(private.is_file(), "before the export is durable the private copy stays authoritative")
                    self.assertEqual(private.stat().st_mode & 0o777, 0o600)
                    self.assertFalse(export.exists(), "a non-durable export name is rolled back")
                else:
                    self.assertEqual((path, reason), (str(export), None))
                    self.assertEqual(export.stat().st_mode & 0o777, 0o400)
                    if name == "unlink":
                        self.assertEqual(private.stat().st_ino, export.stat().st_ino, "a stale private name shares the inode; nothing is duplicated")
                    else:
                        self.assertFalse(private.exists())
                self.assertEqual(self.adapter._read_content({"id": job_id, "content_path": path}), content)
        with self.subTest(fault="different artifact"):
            job_id = make_job()
            planted = self.export_dir / (job_id + ".md")
            planted.write_bytes(b"planted different artifact")
            self.assertEqual(publish(job_id), (None, "export_conflict"))
            self.assertEqual((planted.read_bytes(), (self.job_dir(job_id) / "content").read_bytes()), (b"planted different artifact", content))
            self.assertEqual(self.adapter._read_content({"id": job_id, "content_path": None}), content)
        with self.subTest(fault="identical artifact"):
            job_id = make_job()
            planted = self.export_dir / (job_id + ".md")
            planted.write_bytes(content)
            planted.chmod(0o600)
            self.assertEqual(publish(job_id), (str(planted), None))
            self.assertEqual(planted.stat().st_mode & 0o777, 0o400)
            self.assertFalse((self.job_dir(job_id) / "content").exists(), "one retained copy after adopting an identical artifact")
            self.assertEqual(self.adapter._read_content({"id": job_id, "content_path": str(planted)}), content)
        with self.subTest(fault="directory fsync inside the real worker"):
            instance = self.room("fsync-fault")
            with mock.patch.dict(os.environ, {"DEEPSEEK_FIXTURE_FAULT": "fsync_directories"}):
                terminal = self.run_to_terminal(instance, "Directory fsync fails after the link", "f1", {"kind": "ok"})
            self.assertEqual((terminal["state"], terminal["content_path"], terminal["export_reason"]), ("completed", None, "export_unavailable"))
            result = instance.result(terminal["job_id"])
            self.assertEqual((result["text"], result["content_path"], result["export_reason"]), ("Hello, wörld OK", None, "export_unavailable"))
            self.assertTrue((self.job_dir(terminal["job_id"]) / "content").is_file())
            self.assertEqual(list((self.home / "deepseek" / "exports" / "fsync-fault").iterdir()), [])

    def test_worker_log_is_bounded_within_the_metadata_budget(self):
        sink_path = self.base / "bounded.log"
        fd = os.open(sink_path, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            sink = adapter._BoundedLog(fd, 200)
            self.assertEqual(sink.write(b"x" * 150), 150)
            self.assertEqual(sink.write(b"y" * 150), 150)
            self.assertEqual(sink.write(b"z" * 150), 150)
        finally:
            os.close(fd)
        data = sink_path.read_bytes()
        marker = adapter._BoundedLog.MARKER
        self.assertEqual(data, b"x" * (200 - len(marker)) + marker, "the bound includes the single truncation marker")
        limit = adapter.worker_log_limit(self.adapter.config)
        self.assertEqual(limit, FAST["max_metadata_bytes"] - FAST["max_wire_tail_bytes"] - adapter.METADATA_RESERVE_BYTES)
        self.assertEqual(self.adapter.health()["bounds"]["worker_log_bytes"], limit)
        instance = self.room("log-spam")
        with mock.patch.dict(os.environ, {"DEEPSEEK_FIXTURE_FAULT": "log_spam"}):
            terminal = self.run_to_terminal(instance, "A chatty worker", "s1", {"kind": "ok"})
        self.assertEqual(terminal["state"], "completed", terminal)
        log = self.job_dir(terminal["job_id"]) / "worker.log"
        self.assertLessEqual(log.stat().st_size, limit)
        self.assertGreater(log.stat().st_size, limit - 4096, "the log fills up to its bound before dropping output")
        self.assertIn(b"truncated at its configured bound", log.read_bytes())
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        row = instance.ledger.job(terminal["job_id"])
        self.assertGreaterEqual(row["retained_bytes"], log.stat().st_size, "terminal accounting includes the log")
        self.assertLessEqual(row["retained_bytes"], adapter.reservation_bytes(instance.config))

    def test_log_marker_probe_bound_and_conflict_read_edges(self):
        marker = adapter._BoundedLog.MARKER
        for limit, expected in ((0, b""), (10, b""), (len(marker), marker), (len(marker) + 5, b"xxxxx" + marker)):
            with self.subTest(limit=limit):
                sink_path = self.base / f"bounded-{limit}.log"
                fd = os.open(sink_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                try:
                    sink = adapter._BoundedLog(fd, limit)
                    self.assertEqual(sink.write(b"x" * 500), 500)
                    self.assertEqual(sink.write(b"y" * 500), 500)
                finally:
                    os.close(fd)
                self.assertEqual(sink_path.read_bytes(), expected)
                self.assertLessEqual(sink_path.stat().st_size, limit, "the bound holds even below the marker size")
        probes = self.home / "deepseek" / "probes"
        for index in range(5):
            (probes / f"2026010{index}T000000Z-{'0' * 31}{index}.json").write_text(json.dumps({"kind": "deepseek_probe", "state": "completed"}))
        newest = adapter.latest_probe(probes)
        self.assertEqual(newest["receipt"], f"20260104T000000Z-{'0' * 31}4.json")
        with mock.patch.object(adapter, "MAX_PROBE_ENTRIES", 3):
            bounded = adapter.latest_probe(probes)
            self.assertEqual((bounded["receipt"], bounded["unavailable"], bounded["unavailable_reason"]), (None, True, "probe_directory_exceeds_bound"))
            self.assertEqual(self.adapter.health()["latest_probe"]["unavailable_reason"], "probe_directory_exceeds_bound")
        content = b"synthetic completed answer\n"
        job_id = uuid.uuid4().hex
        fd = self.adapter._job_fd(job_id, create=True)
        try:
            adapter.write_below(fd, "content", content)
            planted = self.export_dir / (job_id + ".md")
            planted.write_bytes(content)
            real = adapter.read_below

            def flaky(base_fd, parts, *args, **kwargs):
                if parts and parts[0].endswith(".md"):
                    raise OSError(5, "synthetic read failure on the existing artifact")
                return real(base_fd, parts, *args, **kwargs)
            with mock.patch.object(adapter, "read_below", flaky):
                self.assertEqual(self.adapter._publish(fd, job_id, adapter.sha(content)), (None, "export_conflict"))
        finally:
            os.close(fd)
        self.assertEqual((self.job_dir(job_id) / "content").read_bytes(), content, "the private copy stays authoritative")
        self.assertEqual(planted.read_bytes(), content, "the unverifiable artifact is never overwritten")
        self.assertEqual(self.adapter._read_content({"id": job_id, "content_path": None}), content)

    def test_probe_cli_runs_without_a_tty_but_refuses_a_tampered_inventory(self):
        self.fake.scenario = {"kind": "ok", "answer": "OK"}
        command = [sys.executable, str(self.boot), str(self.fake.port), ADAPTER, "probe", "--home", str(self.home), "--room", self.room_id, "--config", str(self.config_path)]
        process = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(process.returncode, 0, process.stderr)
        receipt = json.loads(process.stdout)
        self.assertEqual((receipt["kind"], receipt["state"], receipt["observed_model"], receipt["http_status"]), ("deepseek_probe", "completed", adapter.DEFAULT_MODEL, 200))
        self.assertEqual(len(self.fake.requests), 1, "exactly one paid request")
        self.assertNotIn(SYNTHETIC_KEY, process.stdout + process.stderr)
        self.assertEqual(len(list((self.home / "deepseek" / "probes").glob("*.json"))), 1)
        room_root = self.base / "room-root"
        room_root.mkdir(mode=0o700)
        inventory = {"provider": "deepseek", "room_id": self.room_id, "export_dir": str(self.export_dir),
                     "files": {str(self.config_path): adapter.sha(self.config_path.read_bytes()), ADAPTER: "0" * 64}}
        (room_root / "settings.json").write_text(json.dumps({"provider_inventory": inventory}))
        process = subprocess.run(command + ["--room-root", str(room_root)], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60,
                                 env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(process.returncode, 2)
        self.assertIn("inventory_mismatch", process.stderr)
        self.assertEqual(process.stdout, "")
        self.assertEqual(len(self.fake.requests), 1, "a tampered snapshot never probes")

    def test_set_key_walks_ancestors_before_reading_the_secret(self):
        root = self.base / "key-root"
        root.mkdir(mode=0o700)
        real = root / "real"
        real.mkdir(mode=0o700)
        (root / "alias").symlink_to(real, target_is_directory=True)
        prompts = []

        def reader(prompt):
            prompts.append(prompt)
            return "sk-synthetic-written"
        with self.assertRaises(adapter.AdapterError) as alias:
            adapter.set_key(self.home, root / "alias" / "secrets" / "deepseek-api-key", reader=reader, interactive=True)
        self.assertEqual((alias.exception.code, prompts), ("key_unsafe_ancestor", []), "refused before any secret is requested")
        self.assertFalse((real / "secrets").exists(), "nothing is created below a refused component")
        loose = root / "loose"
        loose.mkdir()
        loose.chmod(0o777)
        with self.assertRaises(adapter.AdapterError) as writable:
            adapter.set_key(self.home, loose / "secrets" / "deepseek-api-key", reader=reader, interactive=True)
        self.assertEqual((writable.exception.code, prompts), ("key_unsafe_ancestor", []))
        self.assertFalse((loose / "secrets").exists())
        target = root / "deep" / "er" / "secrets" / "deepseek-api-key"
        self.assertTrue(adapter.set_key(self.home, target, reader=reader, interactive=True)["saved"])
        self.assertEqual(len(prompts), 2)
        for directory in (root / "deep", root / "deep" / "er", target.parent):
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700, directory)
        self.assertEqual((target.stat().st_mode & 0o777, adapter.read_api_key(target)), (0o600, "sk-synthetic-written"))
        linked = target.parent / "linked-key"
        linked.symlink_to(target)
        with self.assertRaises(adapter.AdapterError) as through_link:
            adapter.set_key(self.home, linked, rotate=True, reader=lambda prompt: "sk-through-link", interactive=True)
        self.assertEqual(through_link.exception.code, "key_unsafe")
        self.assertEqual(adapter.read_api_key(target), "sk-synthetic-written", "a symlinked key path never rewrites its target")
        target.parent.chmod(0o750)
        with self.assertRaises(adapter.AdapterError) as loosened:
            adapter.set_key(self.home, target, rotate=True, reader=lambda prompt: "sk-rotated", interactive=True)
        self.assertEqual(loosened.exception.code, "key_unsafe_ancestor", "a loosened key directory is refused, never repaired")
        target.parent.chmod(0o700)
        self.assertTrue(adapter.set_key(self.home, target, rotate=True, reader=lambda prompt: "sk-rotated", interactive=True)["rotated"])
        self.assertEqual(adapter.read_api_key(target), "sk-rotated")

    def test_changed_or_tampered_artifacts_are_detected_not_trusted(self):
        terminal = self.run_to_terminal(self.adapter, "Digest check", "d1", {"kind": "ok"})
        export = Path(terminal["content_path"])
        export.chmod(0o600)
        export.write_text("edited after completion")
        with self.assertRaises(adapter.AdapterError) as changed:
            self.adapter.result(terminal["job_id"])
        self.assertEqual(changed.exception.code, "content_changed")
        with self.assertRaises(adapter.AdapterError) as bad_range:
            self.adapter.result(terminal["job_id"], -1)
        self.assertEqual(bad_range.exception.code, "range_invalid")
        for value in (adapter.MAX_RESULT_CHARS + 1, 0, True):
            with self.assertRaises(adapter.AdapterError):
                self.adapter.result(terminal["job_id"], 0, value)

    def test_status_argument_discipline(self):
        for arguments, code in ((("0" * 32, False, 10), "wait_required"), (("0" * 32, True, 50), "timeout_invalid"), (("0" * 32, True, 0), "timeout_invalid"),
                                (("0" * 32, True, True), "timeout_invalid"), (("0" * 32, True, float("nan")), "timeout_invalid"),
                                (("../x", True, 1), "job_id_invalid"), (("0" * 32, True, 1), "job_unknown")):
            with self.subTest(arguments=arguments), self.assertRaises(adapter.AdapterError) as refused:
                self.adapter.status(*arguments)
            self.assertEqual(refused.exception.code, code)

    def test_set_key_requires_a_tty_and_writes_private_files_without_echo(self):
        home = self.base / "fresh-home"
        target = home / "secrets" / "deepseek-api-key"
        with self.assertRaises(adapter.AdapterError) as tty:
            adapter.set_key(home, target, reader=lambda prompt: "sk-new", interactive=False)
        self.assertEqual(tty.exception.code, "tty_required")
        with self.assertRaises(adapter.AdapterError) as mismatch:
            adapter.set_key(home, target, reader=iter(["sk-one", "sk-two"]).__next__ if False else lambda prompt: "sk-one" if "Repeat" not in prompt else "sk-two", interactive=True)
        self.assertEqual(mismatch.exception.code, "key_mismatch")
        self.assertFalse(target.exists())
        with self.assertRaises(adapter.AdapterError) as invalid:
            adapter.set_key(home, target, reader=lambda prompt: "sk with space", interactive=True)
        self.assertEqual(invalid.exception.code, "key_invalid")
        saved = adapter.set_key(home, target, reader=lambda prompt: "sk-new-synthetic", interactive=True)
        self.assertTrue(saved["saved"])
        self.assertEqual((home.stat().st_mode & 0o777, target.parent.stat().st_mode & 0o777, target.stat().st_mode & 0o777), (0o700, 0o700, 0o600))
        self.assertEqual(target.read_text(), "sk-new-synthetic\n")
        with self.assertRaises(adapter.AdapterError) as exists:
            adapter.set_key(home, target, reader=lambda prompt: "sk-replacement", interactive=True)
        self.assertEqual(exists.exception.code, "key_exists")
        self.assertTrue(adapter.set_key(home, target, rotate=True, reader=lambda prompt: "sk-replacement", interactive=True)["rotated"])
        self.assertEqual(adapter.read_api_key(target), "sk-replacement")
        process = subprocess.run([sys.executable, ADAPTER, "set-key", "--home", str(home)], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        self.assertEqual(process.returncode, 2)
        self.assertIn("tty_required", process.stderr)
        self.assertEqual(process.stdout, "")

    def test_resolve_cli_refuses_without_a_tty_and_health_cli_reads_nothing_private(self):
        note = self.base / "note.md"
        note.write_text("User decision text")
        process = subprocess.run([sys.executable, ADAPTER, "resolve", "--home", str(self.home), "--room", self.room_id, "--job", "0" * 32, "--note-file", str(note)],
                                 stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        self.assertEqual(process.returncode, 2)
        self.assertIn("tty_required", process.stderr)
        process = subprocess.run([sys.executable, ADAPTER, "health", "--home", str(self.home), "--room", self.room_id, "--config", str(self.config_path)],
                                 capture_output=True, text=True, timeout=30)
        self.assertEqual(process.returncode, 0, process.stderr)
        health = json.loads(process.stdout)
        self.assertEqual(health["provider"], "deepseek")
        self.assertNotIn(SYNTHETIC_KEY, process.stdout)
        self.assertEqual(self.fake.requests, [])


class McpTransportTests(AdapterFixture):
    def setUp(self):
        super().setUp()
        self.servers = []

    def tearDown(self):
        for server in self.servers:
            if server.poll() is None:
                server.stdin.close()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)
            server.stdout.close()
            server.stderr.close()
        super().tearDown()

    def start(self, room_root=None, room_id=None, env=None):
        command = [sys.executable, str(self.boot), str(self.fake.port), ADAPTER, "serve", "--home", str(self.home), "--room", room_id or self.room_id,
                   "--config", str(self.config_path)]
        if room_root is not None:
            command += ["--room-root", str(room_root)]
        server = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **(env or {})})
        self.servers.append(server)
        return server

    def request(self, server, method, params=None, identifier=1):
        value = {"jsonrpc": "2.0", "id": identifier, "method": method}
        if params is not None:
            value["params"] = params
        server.stdin.write(json.dumps(value).encode() + b"\n")
        server.stdin.flush()
        line = server.stdout.readline()
        self.assertTrue(line, "MCP exited without a response")
        return json.loads(line)

    def tool(self, server, name, arguments=None):
        response = self.request(server, "tools/call", {"name": name, "arguments": arguments or {}})
        self.assertNotIn("error", response, response)
        content = json.loads(response["result"]["content"][0]["text"])
        if not response["result"]["isError"]:
            self.assertEqual(response["result"]["structuredContent"], content)
        return response["result"]["isError"], content

    def test_initialize_list_and_call_round_trips_keep_stdout_protocol_only(self):
        server = self.start()
        initialized = self.request(server, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "fixture", "version": "1"}})
        self.assertEqual(initialized["result"]["serverInfo"], {"name": "deepseek-delegate", "version": "0.3.0"})
        self.assertEqual(initialized["result"]["protocolVersion"], "2025-06-18")
        listed = self.request(server, "tools/list")["result"]["tools"]
        self.assertEqual({tool["name"] for tool in listed}, {"deepseek_submit", "deepseek_ask", "deepseek_status", "deepseek_result", "deepseek_cancel", "deepseek_health"})
        self.assertTrue(all(tool["inputSchema"]["additionalProperties"] is False for tool in listed))
        submit_schema = next(tool for tool in listed if tool["name"] == "deepseek_submit")["inputSchema"]
        self.assertNotIn("max_tokens", submit_schema["properties"])
        self.assertNotIn("reasoning_effort", submit_schema["properties"])
        self.assertEqual(self.request(server, "ping", identifier="p")["result"], {})
        error, health = self.tool(server, "deepseek_health")
        self.assertFalse(error)
        self.assertEqual(health["model"], adapter.DEFAULT_MODEL)
        error, refused = self.tool(server, "deepseek_submit", {"task": "x", "request_id": "m1", "max_tokens": 5})
        self.assertTrue(error)
        self.assertEqual(refused["code"], "arguments_invalid")
        error, submitted = self.tool(server, "deepseek_submit", {"task": "Via MCP", "request_id": "m1"})
        self.assertFalse(error, submitted)
        error, status = self.tool(server, "deepseek_status", {"job_id": submitted["job_id"], "timeout_s": 20})
        self.assertFalse(error)
        self.assertEqual(status["state"], "completed", status)
        error, result = self.tool(server, "deepseek_result", {"job_id": submitted["job_id"]})
        self.assertEqual(result["text"], "Hello, wörld OK")
        error, polled = self.tool(server, "deepseek_status", {"job_id": submitted["job_id"], "wait": False})
        self.assertTrue(error)
        self.assertEqual(polled["code"], "wait_required")
        error, overlong = self.tool(server, "deepseek_status", {"job_id": submitted["job_id"], "timeout_s": 50})
        self.assertTrue(error)
        self.assertEqual(overlong["code"], "timeout_invalid")
        error, unknown = self.tool(server, "deepseek_nope", {})
        self.assertTrue(error)
        for data, code in ((b"{broken\n", -32700), (b"[]\n", -32600), (b'{"jsonrpc":"2.0","id":2,"method":"missing"}\n', -32601),
                           (b'{"jsonrpc":"2.0","id":2,"method":"ping","params":[]}\n', -32602)):
            server.stdin.write(data)
            server.stdin.flush()
            self.assertEqual(json.loads(server.stdout.readline())["error"]["code"], code)
        server.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "deepseek_submit", "arguments": {"task": "notification", "request_id": "n"}}}).encode() + b"\n")
        server.stdin.flush()
        self.assertEqual(self.request(server, "ping", identifier="after")["id"], "after")
        self.assertEqual(len(self.fake.requests), 1, "a notification never submits")
        self.assertEqual(self.fake.requests[0]["headers"]["Authorization"], "Bearer " + SYNTHETIC_KEY)
        server.stdin.close()
        self.assertEqual(server.wait(timeout=5), 0)
        self.assertEqual(server.stderr.read(), b"")
        self.assertEqual(server.stdout.read(), b"", "stdout carried nothing but JSON-RPC")

    def test_serve_refuses_a_tampered_inventory(self):
        room_root = self.base / "room-root"
        room_root.mkdir(mode=0o700)
        inventory = {"provider": "deepseek", "room_id": self.room_id, "export_dir": str(self.export_dir),
                     "files": {str(self.config_path): adapter.sha(self.config_path.read_bytes()), ADAPTER: "0" * 64}}
        (room_root / "settings.json").write_text(json.dumps({"provider_inventory": inventory}))
        server = self.start(room_root=room_root)
        self.assertEqual(server.wait(timeout=20), 3)
        self.assertIn(b"adapter_source_mismatch", server.stderr.read())
        inventory["files"][ADAPTER] = adapter.sha(Path(ADAPTER).read_bytes())
        (room_root / "settings.json").write_text(json.dumps({"provider_inventory": inventory}))
        server = self.start(room_root=room_root)
        error, health = self.tool(server, "deepseek_health")
        self.assertFalse(error)
        self.assertTrue(health["integrity"]["verified"], health["integrity"])
        self.assertEqual(health["export_dir"]["path"], str(self.export_dir))
        error, submitted = self.tool(server, "deepseek_submit", {"task": "Integrity via MCP", "request_id": "i1"})
        self.assertFalse(error, submitted)
        error, status = self.tool(server, "deepseek_status", {"job_id": submitted["job_id"], "timeout_s": 20})
        self.assertEqual((status["state"], status["integrity"]["verified"]), ("completed", True), status)
        self.config_path.write_text(json.dumps({**FAST, "ask_effort": "none"}))  # a change after startup is reported, never hidden
        error, status = self.tool(server, "deepseek_status", {"job_id": submitted["job_id"], "timeout_s": 1})
        self.assertEqual((status["integrity"]["verified"], status["integrity"]["reason"]), (False, "config_mismatch"))
        error, health = self.tool(server, "deepseek_health")
        self.assertEqual((health["integrity"]["verified"], health["integrity"]["startup"]["verified"]), (False, True))
        self.config_path.write_text(json.dumps(FAST))
        error, health = self.tool(server, "deepseek_health")
        self.assertTrue(health["integrity"]["verified"], "the exact pinned bytes restore the integrity verdict")

    def test_serve_emits_utf8_regardless_of_the_inherited_stdout_encoding(self):
        server = self.start(env={"PYTHONIOENCODING": "ascii", "LC_ALL": "C", "PYTHONCOERCECLOCALE": "0"})
        error, submitted = self.tool(server, "deepseek_submit", {"task": "Unicode answer", "request_id": "u1"})
        self.assertFalse(error, submitted)
        error, status = self.tool(server, "deepseek_status", {"job_id": submitted["job_id"], "timeout_s": 20})
        self.assertEqual(status["state"], "completed", status)
        error, result = self.tool(server, "deepseek_result", {"job_id": submitted["job_id"]})
        self.assertFalse(error, result)
        self.assertEqual(result["text"], "Hello, wörld OK", "the non-ASCII answer crosses the transport as UTF-8")
        self.assertEqual(self.request(server, "ping", identifier=9)["result"], {}, "the server survives the non-ASCII response")
        listed = self.request(server, "tools/list")["result"]["tools"]
        schemas = {tool["name"]: tool["inputSchema"]["properties"] for tool in listed}
        self.assertEqual(schemas["deepseek_status"]["wait"], {"type": "boolean", "const": True}, "the schema advertises exactly what the tool accepts")
        self.assertEqual(schemas["deepseek_status"]["timeout_s"], {"type": "number", "exclusiveMinimum": 0, "maximum": adapter.MAX_WAIT_SECONDS})
        self.assertEqual(schemas["deepseek_submit"]["context_path"]["maxItems"], adapter.DEFAULTS["max_context_files"])


class CrashAccountingTests(AdapterFixture):
    """Durable accounting at the crash boundaries Astra reproduced: admission before the request files are written,
    and a worker lost after a durable export but before its terminal commit."""

    def artifact_bytes(self, job_id):
        return sum(path.stat().st_size for path in self.job_dir(job_id).iterdir() if path.is_file())

    def test_admission_commits_ownership_before_any_artifact_and_relabels_storage_failures(self):
        reservation = adapter.reservation_bytes(self.adapter.config)
        real = adapter.write_below

        class ProcessLoss(BaseException):
            """A crash between the durable row and the request files: no handler catches it."""

        def crashing(fd, name, data, *args, **kwargs):
            if name == "request.json":
                raise ProcessLoss()
            return real(fd, name, data, *args, **kwargs)
        forbidden = mock.patch.object(adapter.Adapter, "_spawn_worker", side_effect=AssertionError("no worker before the input is complete"))
        with forbidden, mock.patch.object(adapter, "write_below", crashing), self.assertRaises(ProcessLoss):
            self.submit("X" * 1500, "crash-1")
        rows = self.adapter.ledger.active(self.room_id)
        self.assertEqual([row["state"] for row in rows], ["queued"], "the durable row was committed before the request text")
        job_id = rows[0]["id"]
        job_dir = self.job_dir(job_id)
        self.assertTrue((job_dir / "request-text").is_file())
        self.assertFalse((job_dir / "request.json").exists())
        self.assertEqual((rows[0]["reserved_bytes"], rows[0]["retained_bytes"]), (reservation, 0))
        self.assertGreaterEqual(rows[0]["reserved_bytes"] + rows[0]["retained_bytes"], self.artifact_bytes(job_id), "no unowned bytes at the crash seam")
        with mock.patch.object(adapter, "STARTUP_GRACE_SECONDS", 0.0):
            relabelled = self.adapter.status(job_id, True, 0.5)
        self.assertEqual((relabelled["state"], relabelled["error_code"], relabelled["possibly_billed"]), ("not_started", "not_launched", False))
        row = self.adapter.ledger.job(job_id)
        self.assertEqual((row["retained_bytes"], row["reserved_bytes"]), (self.artifact_bytes(job_id), 0), "the relabel measures exactly what was left behind")
        with forbidden, mock.patch.object(adapter, "write_below", crashing), self.assertRaises(ProcessLoss):
            self.submit("Y" * 1500, "crash-2")
        incomplete = self.adapter.ledger.active(self.room_id)[0]["id"]
        lease = os.open(self.job_dir(incomplete) / "worker.lock", os.O_RDWR | os.O_NOFOLLOW)
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.adapter.run_worker(incomplete, lease), 0)  # a worker facing incomplete input never sends
        finally:
            os.close(lease)
        row = self.adapter.ledger.job(incomplete)
        self.assertEqual((row["state"], row["error_code"], row["possibly_billed"], row["reserved_bytes"]), ("not_started", "request_unreadable", 0, 0))
        self.assertEqual(row["retained_bytes"], self.artifact_bytes(incomplete))
        self.assertEqual(self.fake.requests, [])
        failing = mock.patch.object(adapter, "write_below", side_effect=OSError(28, "synthetic storage failure"))
        with forbidden, failing, self.assertRaises(adapter.AdapterError) as refused:
            self.submit("Z" * 1500, "crash-3")
        self.assertEqual(refused.exception.code, "request_write_failed")
        failed_job = refused.exception.extra["job_id"]
        row = self.adapter.ledger.job(failed_job)
        self.assertEqual((row["state"], row["error_code"], row["reserved_bytes"], row["retained_bytes"]), ("not_started", "request_write_failed", 0, self.artifact_bytes(failed_job)))
        self.assertEqual(self.adapter.ledger.active(self.room_id), [])
        storage = self.adapter.ledger.storage()
        self.assertEqual(storage["reserved_bytes"], 0)
        self.assertGreaterEqual(storage["retained_bytes"], sum(self.artifact_bytes(row["id"]) for row in (self.adapter.ledger.job(j) for j in (job_id, incomplete, failed_job))))
        self.fake.scenario = {"kind": "ok"}
        again = self.submit("Z" * 1500, "crash-4")  # a proven-unsent payload may be submitted again under a new request_id
        self.assertEqual(self.wait(again["job_id"])["state"], "completed")

    def test_unknown_delivery_after_a_durable_export_is_accounted_never_adopted(self):
        with mock.patch.object(self.adapter, "_spawn_worker", lambda job_id, job_fd, lease_fd: None):
            submitted = self.submit("Exported then lost", "e1")
        job_id = submitted["job_id"]
        content = b"X" * 3000
        job_fd = self.adapter._job_fd(job_id)
        try:
            adapter.write_below(job_fd, "content", content)
            with self.adapter.ledger.transaction() as db:
                db.execute("UPDATE jobs SET state=?,streaming_at=?,possibly_billed=1 WHERE id=?", ("streaming", "2000-01-01T00:00:00+00:00", job_id))
            exported, reason = self.adapter._publish(job_fd, job_id, adapter.sha(content))
            self.assertIsNone(reason)
            self.assertFalse((self.job_dir(job_id) / "content").exists(), "the export is the single durable copy")
            with mock.patch.object(adapter, "STARTUP_GRACE_SECONDS", 0.0):
                refreshed = self.adapter.status(job_id, True, 0.5)
            self.assertEqual((refreshed["state"], refreshed["error_code"], refreshed["content_path"], refreshed["possibly_billed"]),
                             ("unknown_delivery", "worker_vanished", None, True), "the export is accounted, never adopted as a result")
            row = self.adapter.ledger.job(job_id)
            actual = self.adapter._retained(job_fd) + Path(exported).stat().st_size
            self.assertEqual(row["reserved_bytes"], 0)
            self.assertGreaterEqual(row["retained_bytes"], actual, "the durable export counts against retention")
            self.assertLessEqual(row["retained_bytes"], adapter.reservation_bytes(self.adapter.config))
        finally:
            os.close(job_fd)
        self.assertTrue(refreshed["stops_room_lane"])
        with self.assertRaises(adapter.AdapterError) as refused:
            self.adapter.result(job_id)
        self.assertEqual(refused.exception.code, "result_unavailable")
        self.assertEqual(Path(exported).read_bytes(), content, "the durable copy stays on disk for inspection")
        other = self.room("planted")
        with mock.patch.object(other, "_spawn_worker", lambda job_id, job_fd, lease_fd: None):
            planted = other.submit("Symlink at the export name", "e2")["job_id"]
        (other.export_dir / (planted + ".md")).symlink_to(self.base / "worktree" / "module.py")
        self.assertEqual(other._export_bytes(planted, "planted"), 0, "a symlink at the export name is never followed or counted")
        (other.export_dir / (planted + ".md")).unlink()
        (other.export_dir / (planted + ".md")).write_bytes(b"planted artifact under the job name")
        self.assertEqual(other._export_bytes(planted, "planted"), len(b"planted artifact under the job name"), "a regular owned file under the name is counted conservatively")
        self.assertEqual(other._export_bytes(planted, "../planted"), 0, "an unsafe room component is never walked")

    def test_a_relabel_observed_from_another_room_counts_the_owning_rooms_export(self):
        owner, observer = self.room("owner-room"), self.room("observer-room")
        with mock.patch.object(owner, "_spawn_worker", lambda job_id, job_fd, lease_fd: None):
            job_id = owner.submit("Exported then lost elsewhere", "x1")["job_id"]
        content = b"Y" * 2500
        job_fd = owner._job_fd(job_id)
        try:
            adapter.write_below(job_fd, "content", content)
            with owner.ledger.transaction() as db:
                db.execute("UPDATE jobs SET state=?,streaming_at=?,possibly_billed=1 WHERE id=?", ("streaming", "2000-01-01T00:00:00+00:00", job_id))
            exported, reason = owner._publish(job_fd, job_id, adapter.sha(content))
            self.assertIsNone(reason)
            with mock.patch.object(adapter, "STARTUP_GRACE_SECONDS", 0.0):
                observer._refresh_all()  # host-wide observation from a different room's adapter
            row = owner.ledger.job(job_id)
            self.assertEqual((row["state"], row["content_path"]), ("unknown_delivery", None))
            self.assertGreaterEqual(row["retained_bytes"], owner._retained(job_fd) + Path(exported).stat().st_size, "the owning room's export is counted")
        finally:
            os.close(job_fd)


class EncodedRecordBoundTests(AdapterFixture):
    """Every metadata record is bounded on its encoded bytes and the reserve is the sum of those bounds."""

    def test_reserve_is_derived_from_encoded_record_bounds_and_budgets_are_unchanged(self):
        self.assertEqual(adapter.METADATA_RESERVE_BYTES, 65536, "the configured budgets and the worker.log bound do not move")
        self.assertEqual(adapter.METADATA_RESERVE_BYTES, adapter.MAX_REQUEST_RECORD_BYTES + adapter.MAX_META_RECORD_BYTES
                         + adapter.MAX_RESOLUTION_RECORD_BYTES + adapter.SMALL_RECORDS_RESERVE_BYTES)
        self.assertEqual(adapter.worker_log_limit(adapter.validate_config({}, "/private/home")), 917504)
        usage = {field: adapter.MAX_USAGE_COUNTER for field in adapter.USAGE_FIELDS}
        usage.update({outer: {inner: adapter.MAX_USAGE_COUNTER for inner in fields} for outer, fields in adapter.USAGE_DETAIL_FIELDS.items()})
        widest = {"job_id": "f" * 32, "execution_id": "f" * 32, "worker_pid": 2 ** 31, "finished_at": adapter.now(), "content_path": "/" + "p" * 4095,
                  "export_reason": "export_unavailable", "state": "failed_after_send", "error_code": "http_999_unvalidated", "error_type": "unrecognized",
                  "detail": "X" * 64, "http_status": 999, "observed_model": "m" * 128, "finish_reason": "insufficient_system_resource", "usage": usage,
                  "usage_source": "usage_only_chunk", "content_bytes": 2 ** 40, "reasoning_bytes": 2 ** 40, "wire_bytes": 2 ** 40, "content_sha256": "0" * 64,
                  "remote_outcome": "completed", "possibly_billed": True, "availability": "provider_unavailable"}
        encoded = (json.dumps(widest, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        self.assertLessEqual(len(encoded), adapter.MAX_META_RECORD_BYTES, "the widest meta.json fits its bound")
        heartbeat = json.dumps({"job_id": "f" * 32, "execution_id": "f" * 32, "reported_at": adapter.now(), "pid": 2 ** 31}, indent=2)
        self.assertLessEqual(2 * len(heartbeat) + len(json.dumps({"requested_at": adapter.now()}, indent=2)), adapter.SMALL_RECORDS_RESERVE_BYTES)
        self.assertEqual(adapter._clean_usage({"prompt_tokens": adapter.MAX_USAGE_COUNTER, "completion_tokens": adapter.MAX_USAGE_COUNTER + 1, "total_tokens": -1,
                                               "prompt_tokens_details": {"cached_tokens": 10 ** 40, "audio_tokens": 3}}),
                         {"prompt_tokens": adapter.MAX_USAGE_COUNTER, "prompt_tokens_details": {"audio_tokens": 3}}, "counters beyond the exact-integer range are unrecognized")
        self.assertIsNone(adapter._clean_usage({"prompt_tokens": 10 ** 4000}))

    def test_provider_controlled_usage_and_resolution_notes_cannot_exceed_their_records(self):
        huge = {"prompt_tokens": 10 ** 4000, "completion_tokens": adapter.MAX_USAGE_COUNTER, "total_tokens": adapter.MAX_USAGE_COUNTER + 1}
        terminal = self.run_to_terminal(self.room("usage-huge"), "Huge counters", "r1", {"kind": "ok", "usage_value": huge})
        self.assertEqual((terminal["state"], terminal["usage"], terminal["usage_source"]), ("completed", {"completion_tokens": adapter.MAX_USAGE_COUNTER}, "final_chunk"))
        meta = self.job_dir(terminal["job_id"]) / "meta.json"
        self.assertLessEqual(meta.stat().st_size, adapter.MAX_META_RECORD_BYTES)
        self.assertNotIn(b"1" + b"0" * 4000, meta.read_bytes())
        stopped = self.run_to_terminal(self.room("bound-res"), "Stop me", "r1", {"kind": "http_error", "status": 502})
        self.assertEqual(stopped["state"], "failed_after_send")
        nul_note = "X" + "\x00" * (adapter.MAX_NOTE_BYTES - 1)
        with self.assertRaises(adapter.AdapterError) as refused:
            adapter.resolve_job(self.home, "bound-res", stopped["job_id"], nul_note, interactive=True)
        self.assertEqual(refused.exception.code, "note_too_large")
        self.assertGreater(refused.exception.extra["record_bytes"], adapter.MAX_RESOLUTION_RECORD_BYTES)
        self.assertFalse(self.adapter.ledger.resolved(stopped["job_id"]), "nothing is recorded for a refused note")
        plain = "Decision: " + "d" * (adapter.MAX_NOTE_BYTES - 10)
        adapter.resolve_job(self.home, "bound-res", stopped["job_id"], plain, interactive=True)
        projection = self.job_dir(stopped["job_id"]) / "resolution.json"
        self.assertLessEqual(projection.stat().st_size, adapter.MAX_RESOLUTION_RECORD_BYTES)
        self.assertEqual(json.loads(projection.read_text())["note"], plain)
        inode = projection.stat().st_ino
        adapter.resolve_job(self.home, "bound-res", stopped["job_id"], "another note", interactive=True)  # duplicate resolution
        self.assertEqual(projection.stat().st_ino, inode, "an identical projection is never rewritten")
        self.assertEqual([path.name for path in self.job_dir(stopped["job_id"]).iterdir() if path.name.startswith("resolution.json.")], [])
        record = {"job_id": stopped["job_id"], "room_id": "bound-res", "note_sha256": adapter.sha(nul_note.encode()), "created_at": adapter.now(), "note": nul_note}
        self.assertTrue(adapter._write_resolution(self.home / "deepseek" / "jobs", stopped["job_id"], record))
        stub = json.loads(projection.read_text())
        self.assertEqual((stub["note"], stub["note_omitted"], stub["note_sha256"]), (None, "encoded_record_exceeds_bound", record["note_sha256"]))
        self.assertLessEqual(projection.stat().st_size, adapter.MAX_RESOLUTION_RECORD_BYTES)
        with mock.patch.object(adapter, "MAX_REQUEST_RECORD_BYTES", 512), self.assertRaises(adapter.AdapterError) as metadata:
            self.room("bound-diag").submit("A task", "d1", diagnosis="\x00" * 200)
        self.assertEqual(metadata.exception.code, "request_metadata_too_large")
        self.assertIn("diagnosis", str(metadata.exception))

    def test_wire_tail_is_redacted_before_it_is_bounded(self):
        config = {**self.adapter.config, "max_wire_tail_bytes": 4096}
        short = "k"
        tail = adapter._bounded_tail(b"k" * 6000, config, short)
        self.assertLessEqual(len(tail), 4096, "a key shorter than the marker cannot grow the stored tail past its bound")
        self.assertNotIn(b"k", tail)
        key = SYNTHETIC_KEY.encode("ascii")
        straddling = key + b"b" * 4090  # the key straddles the front edge of the last 4096 raw bytes
        tail = adapter._bounded_tail(straddling, config, SYNTHETIC_KEY)
        self.assertLessEqual(len(tail), 4096)
        self.assertNotIn(key, tail)
        self.assertFalse(any(tail.startswith(key[-length:]) for length in range(1, len(key))), "no suffix fragment of the key survives at the front edge")
        shrinking = (b"a" * 10 + key) * 3 + key[10:] + b"c" * 4000  # several replacements shrink the window ahead of a fragment
        tail = adapter._bounded_tail(shrinking, config, SYNTHETIC_KEY)
        self.assertNotIn(key, tail)
        self.assertFalse(any(tail.startswith(key[-length:]) for length in range(1, len(key))))
        self.assertEqual(adapter._bounded_tail(b"", config, SYNTHETIC_KEY), b"")
        self.assertEqual(adapter._bounded_tail(b"plain", config, None), b"plain")
        instance = self.room("tail-echo")
        terminal = self.run_to_terminal(instance, "Echo", "r1", {"kind": "http_error", "status": 401, "type": SYNTHETIC_KEY, "code": SYNTHETIC_KEY})
        wire_tail = self.job_dir(terminal["job_id"]) / "wire-tail"
        self.assertLessEqual(wire_tail.stat().st_size, FAST["max_wire_tail_bytes"])
        self.assertNotIn(SYNTHETIC_KEY.encode(), wire_tail.read_bytes())


class StreamTruthTests(AdapterFixture):
    def test_cap_counters_usage_persistence_streaming_stamp_and_close_are_truthful(self):
        capped = self.base / "capped.json"
        capped.write_text(json.dumps({**FAST, "max_content_bytes": 600}))
        instance = adapter.Adapter(self.home, "capped", capped)
        (self.home / "deepseek" / "exports" / "capped").mkdir(mode=0o700)
        terminal = self.run_to_terminal(instance, "Long answer", "r1", {"kind": "big", "tokens": 1400})
        self.assertEqual((terminal["state"], terminal["error_code"]), ("truncated", "content_cap_exceeded"))
        content = self.job_dir(terminal["job_id"]) / "content"
        self.assertEqual(terminal["content_bytes"], content.stat().st_size, "the recorded count is exactly what was retained")
        self.assertLessEqual(terminal["content_bytes"], 600)
        self.assertEqual(terminal["content_sha256"], adapter.sha(content.read_bytes()))
        rejected = self.run_to_terminal(self.room("stamp"), "Rejected", "r1", {"kind": "http_error", "status": 402})
        self.assertEqual(rejected["state"], "rejected_before_generation")
        self.assertIsNone(rejected["streaming_at"], "a refusal before generation never records a streaming instant")
        self.assertIsNotNone(rejected["submitting_at"])
        completed = self.run_to_terminal(self.room("stamp-ok"), "Fine", "r1", {"kind": "ok"})
        self.assertIsNotNone(completed["streaming_at"])
        outcome = {"observed_model": None, "finish_reason": None, "usage": None, "usage_source": "missing"}
        artifacts = self.base / "stream-artifacts"
        artifacts.mkdir(mode=0o700)
        fd = os.open(artifacts, os.O_RDONLY | os.O_DIRECTORY)
        try:
            stream = adapter._Stream(self.adapter.config, fd, SYNTHETIC_KEY, outcome)
            stream.line(chunk({"content": "x"}, finish="stop", usage={"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}).strip())
            self.assertEqual(outcome["usage"], {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6})
            stream.finished = False
            stream.line(chunk({}, usage={}).strip())
            stream.line(chunk({}, usage={"unknown_counter": 1}).strip())
            self.assertEqual((outcome["usage"], outcome["usage_source"]), ({"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}, "final_chunk"),
                             "a later empty or unrecognized usage object never erases captured counters")
            real = os.fsync
            calls = []

            def failing(handle_fd):
                calls.append(handle_fd)
                if len(calls) == 1:  # the content artifact is flushed first; descriptor numbers are reused later, so match by order
                    raise OSError(5, "synthetic flush failure")
                return real(handle_fd)
            with mock.patch.object(adapter.os, "fsync", failing), self.assertRaises(OSError):
                stream.close()
            self.assertTrue(stream.content.closed and stream.reasoning.closed, "both artifacts are closed even when the first flush fails")
            self.assertTrue((artifacts / "wire-tail").is_file(), "the tail is retained even when a flush fails")
        finally:
            os.close(fd)
        second = self.base / "stream-artifacts-2"
        second.mkdir(mode=0o700)
        fd = os.open(second, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fresh = {"observed_model": None, "finish_reason": None, "usage": None, "usage_source": "missing"}
            fresh_stream = adapter._Stream(self.adapter.config, fd, SYNTHETIC_KEY, fresh)
            fresh_stream.line(chunk({}, usage={"unknown_counter": 1}).strip())
            self.assertEqual((fresh["usage"], fresh["usage_source"]), (None, "unrecognized"))
            fresh_stream.close()
        finally:
            os.close(fd)

    def test_result_falls_through_to_the_export_when_the_private_directory_is_gone(self):
        terminal = self.run_to_terminal(self.adapter, "Keep the export", "r1", {"kind": "ok"})
        export = Path(terminal["content_path"])
        import shutil
        shutil.rmtree(self.job_dir(terminal["job_id"]))
        self.assertEqual(self.adapter.result(terminal["job_id"])["text"], "Hello, wörld OK")
        self.assertEqual(self.adapter._read_content({"id": terminal["job_id"], "content_path": None}), export.read_bytes(), "the export name is the only remaining place")
        with self.assertRaises(adapter.AdapterError) as missing:
            self.adapter._read_content({"id": "0" * 32, "content_path": None})
        self.assertTrue(missing.exception.code.endswith("_missing"))


class OwnedSnapshotAndDirectoryTests(AdapterFixture):
    def test_snapshot_reads_are_owned_bounded_and_never_follow_a_device_symlink(self):
        planted = self.base / "planted-adapter.py"
        planted.symlink_to("/dev/zero")
        with self.assertRaises(adapter.AdapterError) as refused:
            adapter.Adapter(self.home, self.room_id, self.config_path, adapter_path=planted)
        self.assertIn(refused.exception.code, ("snapshot_unsafe", "snapshot_oversized"), "a device behind the snapshot name is refused, never read")
        with mock.patch.object(adapter, "MAX_SNAPSHOT_BYTES", 16), self.assertRaises(adapter.AdapterError) as oversized:
            adapter.Adapter(self.home, self.room_id, self.config_path)
        self.assertEqual(oversized.exception.code, "snapshot_oversized")
        room_root = self.base / "room-root"
        room_root.mkdir(mode=0o700)
        inventory = {"provider": "deepseek", "room_id": self.room_id, "export_dir": str(self.export_dir),
                     "files": {str(self.config_path): adapter.sha(self.config_path.read_bytes()), ADAPTER: adapter.sha(Path(ADAPTER).read_bytes())}}
        (room_root / "settings.json").write_text(json.dumps({"provider_inventory": inventory}))
        instance = adapter.Adapter(self.home, self.room_id, self.config_path, room_root=room_root)
        self.assertTrue(instance.integrity["verified"])
        with mock.patch.object(adapter, "MAX_SNAPSHOT_BYTES", 16):
            self.assertEqual(instance._verify_inventory(fresh=True)["reason"], "snapshot_unreadable")
        self.assertTrue(instance._verify_inventory(fresh=True)["verified"])

    def test_private_directories_and_publication_refuse_group_or_other_access(self):
        shared = self.base / "shared"
        shared.mkdir(mode=0o750)
        with self.assertRaises(adapter.AdapterError) as refused:
            adapter.ensure_private_directory(shared)
        self.assertEqual(refused.exception.code, "path_unsafe_mode")
        self.assertIn("chmod 700", str(refused.exception))
        self.assertEqual(shared.stat().st_mode & 0o777, 0o750, "never chmodded")
        self.assertEqual(adapter.ensure_private_directory(self.base / "fresh" / "nested"), self.base / "fresh" / "nested")
        self.assertEqual((self.base / "fresh").stat().st_mode & 0o777, 0o700)
        content = b"synthetic completed answer\n"
        job_id = uuid.uuid4().hex
        fd = self.adapter._job_fd(job_id, create=True)
        try:
            adapter.write_below(fd, "content", content)
            self.export_dir.chmod(0o770)
            try:
                self.assertEqual(self.adapter._publish(fd, job_id, adapter.sha(content)), (None, "export_dir_unsafe"))
            finally:
                self.export_dir.chmod(0o700)
            self.assertEqual((self.job_dir(job_id) / "content").read_bytes(), content, "the private copy stays authoritative")
            self.assertFalse((self.export_dir / (job_id + ".md")).exists())
        finally:
            os.close(fd)
        with mock.patch.object(adapter.os, "scandir", side_effect=OSError(5, "synthetic enumeration failure")):
            bounded = adapter.latest_probe(self.home / "deepseek" / "probes")
        self.assertEqual((bounded["receipt"], bounded["unavailable"], bounded["unavailable_reason"]), (None, True, "probe_directory_unreadable"))


if __name__ == "__main__":
    unittest.main()
