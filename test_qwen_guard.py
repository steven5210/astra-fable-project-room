import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from qwen_guard import GuardError, enforce, parse_message


GUARD = Path(__file__).with_name("qwen_guard.py")
BACKEND = '''import json, os, sys
mode = os.environ.get("FAKE_MODE", "echo")
if mode == "disconnect": sys.exit(7)
if mode == "malformed": print("backend-secret-token", flush=True); sys.exit(0)
if mode == "hang":
    import time
    with open(os.environ["SEEN"], "a") as log: log.write(json.dumps({"pid":os.getpid()}) + "\\n")
    time.sleep(60)
for line in sys.stdin:
    message = json.loads(line)
    with open(os.environ["SEEN"], "a") as log: log.write(line)
    if "id" in message:
        result = {"isError": True, "content": [{"type":"text", "text":"backend error"}]} if mode == "error" else message
        print(json.dumps({"jsonrpc":"2.0", "id":message["id"], "result":result}), flush=True)
'''


def call(name, arguments=None, request_id=1):
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}}}


class GuardTests(unittest.TestCase):
    def run_proxy(self, messages, mode="echo", full_config=False, raw=False):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            backend, seen, config = folder / "backend.py", folder / "seen", folder / "config.json"
            backend.write_text(BACKEND)
            definition = {"command": sys.executable, "args": [str(backend)],
                          "env": {"SEEN": str(seen), "FAKE_MODE": mode}}
            config.write_text(json.dumps({"mcpServers": {"qwen-local": definition}} if full_config else definition))
            data = messages if raw else "".join(json.dumps(x) + "\n" for x in messages)
            result = subprocess.run([sys.executable, str(GUARD), "--config", str(config)],
                                    input=data, text=True, capture_output=True, timeout=10)
            received = [json.loads(x) for x in seen.read_text().splitlines()] if seen.exists() else []
            output = [json.loads(x) for x in result.stdout.splitlines()]
            return result, received, output

    def test_invalid_budgets_never_reach_backend(self):
        invalid = [call("qwen_submit", {"effort": "low"}, "effort"),
                   call("qwen_submit", {"max_tokens": 1024}, "budget"),
                   call("qwen_submit", {"max_tokens": 131072.0}, "float"),
                   call("qwen_submit", {"reasoning_effort": "low"}, "alias"),
                   call("qwen_ask", {"effort": "xhigh"}, "ask"),
                   call("qwen_status", {"wait": False}, "poll"),
                   call("qwen_status", {"timeout_s": 50}, "timeout"),
                   call("qwen_status", {"timeout_s": True}, "boolean")]
        result, received, output = self.run_proxy(invalid)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(received, [])
        self.assertEqual([x["id"] for x in output], [x["id"] for x in invalid])
        self.assertTrue(all(x["result"]["isError"] for x in output))

    def test_fixed_settings_unchanged_and_missing_defaults_filled(self):
        fixed = call("qwen_submit", {"task": "payload", "effort": "xhigh", "max_tokens": 131072,
                                      "context_path": "/tmp/context"}, "fixed")
        messages = [fixed, call("qwen_submit", {"task": "payload"}, "default"),
                    call("qwen_ask", {"question": "quick", "effort": "none"}, "ask"),
                    call("qwen_status", {"job_id": "job"}, "wait"),
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 9, "method": "tools/list", "params": {}}]
        result, received, output = self.run_proxy(messages, full_config=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(received[0], fixed)
        self.assertEqual(received[1]["params"]["arguments"], {"task": "payload", "effort": "xhigh", "max_tokens": 131072})
        self.assertEqual(received[2], messages[2])
        self.assertEqual(received[3]["params"]["arguments"], {"job_id": "job", "wait": True, "timeout_s": 45})
        self.assertEqual(received[4:], messages[4:])
        self.assertEqual([x["id"] for x in output], ["fixed", "default", "ask", "wait", 9])

    def test_backend_mcp_errors_preserved(self):
        result, received, output = self.run_proxy([call("qwen_health")], mode="error")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(output[0]["result"]["isError"])
        self.assertEqual(output[0]["result"]["content"][0]["text"], "backend error")

    def test_malformed_client_fails_closed(self):
        for data in ['not-json-secret\n', '{"jsonrpc":"2.0","id":1,"id":2}\n',
                     '{"jsonrpc":"2.0","id":true}\n', '{"jsonrpc":"2.0"}',
                     '{"jsonrpc":"2.0","value":NaN}\n', '{"jsonrpc":"2.0"}\n',
                     '{"jsonrpc":"2.0","method":12}\n',
                     '{"jsonrpc":"2.0","id":1,"result":{},"error":{}}\n']:
            with self.subTest(data=data):
                result, received, output = self.run_proxy(data, raw=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(received, [])
                self.assertEqual(output, [])
                self.assertNotIn("secret", result.stderr)

    def test_backend_disconnect_or_malformed_output_fails(self):
        for mode in ("disconnect", "malformed"):
            with self.subTest(mode=mode):
                result, _, output = self.run_proxy([], mode=mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(output, [])
                self.assertNotIn("backend-secret", result.stderr)

    def test_invalid_tool_shape_fails(self):
        message = call("qwen_submit")
        message["params"]["arguments"] = []
        with self.assertRaises(GuardError):
            enforce(message)

    def test_backend_is_reaped_when_it_ignores_eof(self):
        result, received, output = self.run_proxy([], mode="hang")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(output, [])
        self.assertEqual(len(received), 1)
        with self.assertRaises(ProcessLookupError):
            os.kill(received[0]["pid"], 0)

    def test_valid_jsonrpc_error_and_notification_are_preserved(self):
        error = {"jsonrpc": "2.0", "id": "req", "error": {"code": -32600, "message": "bad request"}}
        note = {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progress": 1}}
        self.assertEqual(parse_message(json.dumps(error)), error)
        self.assertEqual(parse_message(json.dumps(note)), note)

    def test_enforcement_does_not_mutate_original(self):
        original = call("qwen_submit", {"task": "payload"})
        forward, rejection = enforce(original)
        self.assertIsNone(rejection)
        self.assertNotIn("effort", original["params"]["arguments"])
        self.assertEqual(forward["params"]["arguments"]["effort"], "xhigh")


if __name__ == "__main__":
    unittest.main()
