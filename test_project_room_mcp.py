"""MCP stdio/process tests; fake Claude only, no model or network dependencies."""

import json
import os
import selectors
import subprocess
import sys
import unittest

import project_room
from test_project_room import ProjectFixture, ROOT


class ProjectRoomMcpTests(ProjectFixture):
    def setUp(self):
        super().setUp()
        self.servers = []
        self.server = self.start_server()

    def tearDown(self):
        for server in self.servers:
            if server.poll() is None:
                server.stdin.close()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.terminate()
                    server.wait(timeout=5)
            server.stdout.close()
            server.stderr.close()
        super().tearDown()

    def start_server(self, bootstrap=None):
        command = [sys.executable]
        if bootstrap is not None:
            command.extend(["-c", bootstrap])
        command.append(str(ROOT / "project_room_mcp.py"))
        server = subprocess.Popen(command, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  env={**os.environ, "PROJECT_ROOM_HOME": str(self.home), "PYTHONDONTWRITEBYTECODE": "1"})
        self.servers.append(server)
        return server

    def read_response(self, server=None, timeout=8):
        server = server or self.server
        with selectors.DefaultSelector() as selector:
            selector.register(server.stdout, selectors.EVENT_READ)
            self.assertTrue(selector.select(timeout), "MCP response timed out")
        line = server.stdout.readline()
        self.assertTrue(line, "MCP exited without a JSON-RPC response")
        return json.loads(line)

    def raw(self, data, server=None):
        server = server or self.server
        server.stdin.write(data)
        server.stdin.flush()
        return self.read_response(server)

    def request(self, method, params=None, identifier=1, server=None):
        value = {"jsonrpc": "2.0", "id": identifier, "method": method}
        if params is not None:
            value["params"] = params
        return self.raw(json.dumps(value).encode() + b"\n", server)

    def tool(self, name, arguments=None, server=None):
        response = self.request("tools/call", {"name": name, "arguments": arguments or {}}, server=server)
        self.assertNotIn("error", response, response)
        self.assertFalse(response["result"]["isError"], response)
        content = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(response["result"]["structuredContent"], content)
        return content

    def test_initialize_discovery_and_fake_auth_have_protocol_only_stdout(self):
        initialized = self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "fixture", "version": "1"}})
        self.assertEqual(initialized["result"]["protocolVersion"], "2025-06-18")
        discovered = self.request("tools/list")["result"]["tools"]
        self.assertEqual({tool["name"] for tool in discovered}, set(project_room.TOOL_SCHEMAS))
        self.assertTrue(all(tool["inputSchema"]["additionalProperties"] is False for tool in discovered))
        doctor = self.tool("room_doctor")
        self.assertTrue(doctor["claude_auth"]["loggedIn"])
        self.assertNotIn("DO_NOT_EXPOSE", json.dumps(doctor))
        self.assertEqual(self.calls(), [])

    def test_review_worker_survives_mcp_exit_and_reconnect_reuses_job_and_session(self):
        self.control(wait=True)
        args = {"room_id": self.room_id, "revision": 1, "message": "Review via MCP", "request_id": "mcp-review-1"}
        job = self.tool("room_review_submit", args)
        self.wait_started()
        self.server.stdin.close()
        self.assertEqual(self.server.wait(timeout=5), 0)
        reopened = self.start_server()
        duplicate = self.tool("room_review_submit", args, reopened)
        self.assertEqual(duplicate["id"], job["id"])
        self.assertTrue(duplicate["duplicate"])
        self.assertIn(duplicate["status"], ("queued", "running"))
        (self.base / "release").touch()
        terminal = self.tool("room_job_status", {"job_id": job["id"], "wait_seconds": 15}, reopened)
        self.assertEqual(terminal["status"], "succeeded", terminal)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.tool("room_list", server=reopened)["rooms"][0]["id"], self.room_id)

    def test_parse_and_envelope_errors_do_not_poison_next_request(self):
        cases = [(b"{broken\n", -32700), (b"\xff\n", -32700), (b"[]\n", -32600),
                 (b'{"jsonrpc":"2.0","id":true,"method":"ping"}\n', -32600),
                 (b'{"jsonrpc":"2.0","id":2,"method":"ping","params":[]}\n', -32602),
                 (b'{"jsonrpc":"2.0","id":2,"method":"missing"}\n', -32601),
                 (b'{"jsonrpc":"2.0","id":NaN,"method":"ping"}\n', -32700)]
        for data, code in cases:
            with self.subTest(data=data):
                self.assertEqual(self.raw(data)["error"]["code"], code)
                self.assertEqual(self.request("ping", identifier="still-alive")["result"], {})

    def test_malformed_envelopes_return_null_for_invalid_ids_and_preserve_valid_ids(self):
        for identifier in ({"nested": "id"}, ["id"], True, False):
            for envelope in ({"jsonrpc": "invalid-version", "method": "ping"},
                             {"jsonrpc": "2.0", "method": 17}):
                with self.subTest(identifier=identifier, envelope=envelope):
                    response = self.raw(json.dumps({**envelope, "id": identifier}).encode() + b"\n")
                    self.assertEqual(response["error"]["code"], -32600)
                    self.assertIsNone(response["id"])
        for identifier in ("valid-id", 7, 1.5, None):
            with self.subTest(identifier=identifier):
                response = self.raw(json.dumps({"jsonrpc": "invalid-version", "method": "ping", "id": identifier}).encode() + b"\n")
                self.assertEqual(response["error"]["code"], -32600)
                self.assertEqual(response["id"], identifier)
        # Overflowing JSON is already rejected by the parser. Verify the shared
        # error-response boundary also normalizes nonfinite direct-call IDs.
        from project_room_mcp import error_response
        for identifier in (float("inf"), float("-inf"), float("nan")):
            self.assertIsNone(error_response(identifier, -32600, "Invalid request")["id"])
        self.assertEqual(self.request("ping", identifier="after-invalid-ids")["result"], {})

    def test_nested_id_in_malformed_envelope_never_escapes_or_kills_server(self):
        data = b'{"jsonrpc":"invalid-version","method":"ping","id":' + b"[" * 1500 + b"0" + b"]" * 1500 + b"}\n"
        response = self.raw(data)
        self.assertIn(response["error"]["code"], (-32700, -32600))
        self.assertIsNone(response["id"])
        self.assertEqual(self.request("ping", identifier="after-nested-id")["result"], {})

    def test_invalid_tool_arguments_fail_without_mutating_registry_or_starting_model(self):
        cases = [("room_open", {"project_path": str(self.project), "feature": "Unsafe extra", "unexpected": True}),
                 ("room_spec_put", {"room_id": self.room_id, "revision": True, "content": "Bad revision"}),
                 ("room_review_submit", {"room_id": self.room_id, "revision": 1, "request_id": "missing-message"}),
                 ("room_job_status", {"job_id": "../arbitrary", "wait_seconds": 0}),
                 ("room_job_status", {"job_id": "0" * 32, "wait_seconds": 46}),
                 ("room_open", []), ("missing-tool", {})]
        for name, arguments in cases:
            with self.subTest(name=name, arguments=arguments):
                result = self.request("tools/call", {"name": name, "arguments": arguments})
                self.assertTrue(result["result"]["isError"], result)
        self.assertEqual(len(self.service.room_list()["rooms"]), 1)
        self.assertEqual(self.service.room_status(self.room_id)["review"]["current_revision"], 1)
        self.assertEqual(self.calls(), [])

    def test_notifications_do_not_trigger_mutating_tools_or_emit_responses(self):
        notification = {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "room_spec_put", "arguments":
                        {"room_id": self.room_id, "revision": 2, "content": "Not a request"}}}
        self.server.stdin.write(json.dumps(notification).encode() + b"\n")
        self.server.stdin.flush()
        response = self.request("ping", identifier="after-notification")
        self.assertEqual(response["id"], "after-notification")
        self.assertEqual(self.service.room_status(self.room_id)["review"]["current_revision"], 1)

    def test_oversize_frame_is_drained_without_interpreting_suffix_as_request(self):
        data = b" " * 3_000_010 + b'{"jsonrpc":"2.0","id":8,"method":"ping"}\n'
        self.assertEqual(self.raw(data)["error"]["code"], -32600)
        self.assertEqual(self.request("ping", identifier=9)["id"], 9)

    def test_nested_json_is_rejected_and_server_survives(self):
        data = b"[" * 1500 + b"0" + b"]" * 1500 + b"\n"
        # Decoder nesting limits differ across Python versions. This is valid
        # JSON: if decoded, its array envelope is invalid JSON-RPC (-32600);
        # if the decoder hits its limit, it is a parse error (-32700).
        self.assertIn(self.raw(data)["error"]["code"], (-32700, -32600))
        self.assertEqual(self.request("ping", identifier=10)["id"], 10)

    def test_decoder_recursion_error_returns_parse_error_and_server_survives(self):
        # Exercise the decoder-error recovery independently of the interpreter's
        # native nesting threshold. Removing the runtime RecursionError handler
        # makes this real MCP child exit and this test fail.
        bootstrap = '''import json, runpy, sys
original_loads = json.loads
def fixture_loads(value, *args, **kwargs):
    if value == b'{"jsonrpc":"2.0","id":"decoder-depth-fixture","method":"ping"}\\n':
        raise RecursionError("fixture decoder nesting limit")
    return original_loads(value, *args, **kwargs)
json.loads = fixture_loads
runpy.run_path(sys.argv[1], run_name="__main__")
'''
        server = self.start_server(bootstrap=bootstrap)
        response = self.raw(b'{"jsonrpc":"2.0","id":"decoder-depth-fixture","method":"ping"}\n', server)
        self.assertEqual(response["error"]["code"], -32700)
        self.assertIsNone(response["id"])
        self.assertEqual(self.request("ping", identifier="after-decoder-error", server=server)["result"], {})

    def test_overflowing_json_number_cannot_crash_response_serialization(self):
        response = self.raw(b'{"jsonrpc":"2.0","id":1e999,"method":"ping"}\n')
        self.assertIn(response["error"]["code"], (-32700, -32600))
        self.assertEqual(self.request("ping", identifier=11)["id"], 11)


if __name__ == "__main__":
    unittest.main()
