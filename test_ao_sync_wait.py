import copy
import fcntl
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

import ao_project_room as ao
import project_room
import project_room_mcp
from test_ao_project_room import FakeAO


class FakeClock:
    def __init__(self, on_sleep=None):
        self.now = 0.0
        self.sleeps = []
        self.on_sleep = on_sleep

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        if self.on_sleep:
            self.on_sleep(seconds)


class SyncWaitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        for args in (("init",), ("config", "user.name", "Test"), ("config", "user.email", "test@example.invalid")):
            subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True)
        (self.repo / "feature.txt").write_text("verified behavior\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "fixture"], check=True, capture_output=True)
        self.fake = FakeAO(self.repo)
        self.service = ao.Service(self.root / "state", lambda base: self.fake)
        self.room = self.service.ao_room_open(
            str(self.repo), "test", "project", "User authorized Astra implementation",
            "http://127.0.0.1:1234", workflow="astra_led",
            exception_authorization="User approved this task-scoped Astra exception"
        )["room_id"]
        self.gates = [[sys.executable, "-c", "pass"]]
        self.service.ao_room_spec_put(self.room, 1, "Verify the exact behavior.", self.gates, "Astra approves the authorized scope")

    def prepare_running(self):
        self.service.ao_room_bind(self.room, "engineer", "engineer", "astra", "max")
        self.service.ao_room_send(self.room, "engineer", "Do the scoped work", "first")

    def state_path(self):
        return self.service.root / "rooms" / self.room / "state.json"

    def write_sync_wait_config(self, value):
        path = self.service.root / "config.json"
        config = ao.read(path) if path.exists() else {}
        if not isinstance(config, dict):
            config = {}
        config["sync_wait_max_seconds"] = value
        path.write_text(json.dumps(config))

    def test_zero_and_omitted_wait_preserve_sync_output_without_sleep(self):
        self.prepare_running()
        clock = FakeClock()
        with patch("ao_project_room.time.sleep", side_effect=clock.sleep) as sleep, \
                patch("ao_project_room.time.monotonic", side_effect=clock.monotonic) as monotonic:
            omitted = self.service.ao_room_sync(self.room)
            explicit_zero = self.service.ao_room_sync(self.room, wait_seconds=0)
        self.assertEqual(omitted, explicit_zero)
        self.assertNotIn("wait", explicit_zero)
        sleep.assert_not_called()
        monotonic.assert_not_called()

    def test_running_turn_that_completes_on_second_sync_returns_saved_receipt(self):
        self.prepare_running()
        clock = FakeClock(on_sleep=lambda _: self.fake.finish("engineer"))
        with patch.object(self.service, "_sync_once", wraps=self.service._sync_once) as sync_once, \
                patch("ao_project_room.time.sleep", side_effect=clock.sleep), \
                patch("ao_project_room.time.monotonic", side_effect=clock.monotonic):
            result = self.service.ao_room_sync(self.room, wait_seconds=45)
        self.assertEqual(sync_once.call_count, 2)
        self.assertEqual(result["wait"], {"requested_seconds": 45, "reason": "no_active_request", "settled": True})
        self.assertEqual(result["requests"][0]["state"], "completed")
        request = ao.read(self.state_path())["requests"]["first"]
        receipt_path = self.service.root / "rooms" / self.room / request["receipt"]
        self.assertTrue(receipt_path.is_file())

    def test_one_completed_request_does_not_settle_while_another_is_running(self):
        initial_states = {"first": "running", "second": "running"}
        final_states = {"first": "completed", "second": "running"}
        initial = {"requests": [{"request_id": "first", "state": "running"},
                                {"request_id": "second", "state": "running"}]}
        final = {"requests": [{"request_id": "first", "state": "completed"},
                              {"request_id": "second", "state": "running"}]}
        clock = FakeClock()
        with patch.object(self.service, "_sync_once", side_effect=[(initial, initial_states),
                                                                    (final, final_states)]) as sync_once, \
                patch("ao_project_room.time.sleep", side_effect=clock.sleep), \
                patch("ao_project_room.time.monotonic", side_effect=clock.monotonic):
            result = self.service.ao_room_sync(self.room, wait_seconds=45)
        self.assertEqual(sync_once.call_count, 2)
        self.assertEqual(result["wait"], {"requested_seconds": 45, "reason": "state_changed", "settled": False})
        self.assertEqual([request["state"] for request in result["requests"]], ["completed", "running"])

    def test_uncertain_request_is_not_settled_when_no_request_remains_active(self):
        initial_states = {"first": "running"}
        final_states = {"first": "uncertain"}
        initial = {"requests": [{"request_id": "first", "state": "running"}]}
        final = {"requests": [{"request_id": "first", "state": "uncertain"}]}
        clock = FakeClock()
        with patch.object(self.service, "_sync_once", side_effect=[(initial, initial_states),
                                                                    (final, final_states)]) as sync_once, \
                patch("ao_project_room.time.sleep", side_effect=clock.sleep), \
                patch("ao_project_room.time.monotonic", side_effect=clock.monotonic):
            result = self.service.ao_room_sync(self.room, wait_seconds=45)
        self.assertEqual(sync_once.call_count, 2)
        self.assertEqual(result["wait"], {"requested_seconds": 45, "reason": "no_active_request", "settled": False})
        self.assertEqual(result["requests"][0]["state"], "uncertain")

    def test_running_turn_times_out_within_the_poll_bound(self):
        self.prepare_running()
        clock = FakeClock()
        with patch.object(self.service, "_sync_once", wraps=self.service._sync_once) as sync_once, \
                patch("ao_project_room.time.sleep", side_effect=clock.sleep), \
                patch("ao_project_room.time.monotonic", side_effect=clock.monotonic):
            result = self.service.ao_room_sync(self.room, wait_seconds=45)
        self.assertEqual(result["wait"], {"requested_seconds": 45, "reason": "timeout", "settled": False})
        self.assertLessEqual(sync_once.call_count, math.ceil(45 / ao.AO_SYNC_WAIT_POLL_SECONDS) + 1)
        self.assertEqual(sync_once.call_count, 10)
        self.assertTrue(all(seconds == ao.AO_SYNC_WAIT_POLL_SECONDS for seconds in clock.sleeps))
        self.assertLessEqual(clock.now, 45 + ao.AO_SYNC_WAIT_POLL_SECONDS)
        self.assertNotIn("config_warning", result["wait"])

    def test_configured_wait_cap_allows_long_waits_and_boundary_values(self):
        self.prepare_running()
        self.write_sync_wait_config(600)
        clock = FakeClock()
        with patch.object(self.service, "_sync_once", wraps=self.service._sync_once) as sync_once, \
                patch("ao_project_room.time.sleep", side_effect=clock.sleep), \
                patch("ao_project_room.time.monotonic", side_effect=clock.monotonic):
            result = self.service.ao_room_sync(self.room, wait_seconds=600)
        self.assertEqual(result["wait"], {"requested_seconds": 600, "reason": "timeout", "settled": False})
        self.assertNotIn("config_warning", result["wait"])
        self.assertLessEqual(sync_once.call_count, math.ceil(600 / ao.AO_SYNC_WAIT_POLL_SECONDS) + 1)
        self.assertLessEqual(clock.now, 600 + ao.AO_SYNC_WAIT_POLL_SECONDS)

        clock = FakeClock()
        with patch("ao_project_room.time.sleep", side_effect=clock.sleep), \
                patch("ao_project_room.time.monotonic", side_effect=clock.monotonic):
            result = self.service.ao_room_sync(self.room, wait_seconds=45.01)
        self.assertEqual(result["wait"]["requested_seconds"], 45.01)
        self.assertEqual(result["wait"]["reason"], "timeout")
        with self.assertRaisesRegex(ao.RoomError, "wait_seconds must be finite and between 0 and 600"):
            self.service.ao_room_sync(self.room, wait_seconds=600.01)

    def test_invalid_wait_caps_fall_back_with_warning_and_reject_over_limit(self):
        self.prepare_running()
        path = self.service.root / "config.json"
        invalid_values = (44, 1801, True, "600", 600.5, [])
        for value in invalid_values:
            with self.subTest(value=value):
                path.write_text(json.dumps(value if isinstance(value, list) else {"sync_wait_max_seconds": value}))
                before = self.state_path().read_bytes()
                gets = self.fake.gets
                with self.assertRaisesRegex(ao.RoomError, "wait_seconds must be finite and between 0 and 45"):
                    self.service.ao_room_sync(self.room, wait_seconds=45.01)
                self.assertEqual(self.fake.gets, gets)
                self.assertEqual(self.state_path().read_bytes(), before)
                clock = FakeClock()
                with patch("ao_project_room.time.sleep", side_effect=clock.sleep), \
                        patch("ao_project_room.time.monotonic", side_effect=clock.monotonic):
                    result = self.service.ao_room_sync(self.room, wait_seconds=45)
                self.assertEqual(result["wait"]["reason"], "timeout")
                self.assertEqual(result["wait"]["config_warning"], ao.SYNC_WAIT_MAX_SECONDS_ERROR)
                with patch("ao_project_room.sync_wait_max_seconds", side_effect=AssertionError("config read")):
                    result = self.service.ao_room_sync(self.room, wait_seconds=0)
                self.assertNotIn("wait", result)

    def test_set_interrupt_returns_without_a_second_sync(self):
        self.prepare_running()
        interrupt = threading.Event()
        interrupt.set()
        with patch.object(ao, "SYNC_WAIT_INTERRUPT", interrupt), \
                patch.object(self.service, "_sync_once", wraps=self.service._sync_once) as sync_once:
            result = self.service.ao_room_sync(self.room, wait_seconds=45)
        self.assertEqual(sync_once.call_count, 1)
        self.assertEqual(result["wait"], {"requested_seconds": 45, "reason": "interrupted", "settled": False})

    def test_wait_interrupt_event_can_interrupt_after_two_polls(self):
        self.prepare_running()

        class Interrupt:
            def __init__(self):
                self.wait_calls = 0

            def is_set(self):
                return False

            def wait(self, _seconds):
                self.wait_calls += 1
                return self.wait_calls == 3

        interrupt = Interrupt()
        with patch.object(ao, "SYNC_WAIT_INTERRUPT", interrupt), \
                patch.object(self.service, "_sync_once", wraps=self.service._sync_once) as sync_once, \
                patch("ao_project_room.time.sleep") as sleep:
            result = self.service.ao_room_sync(self.room, wait_seconds=45)
        self.assertEqual(interrupt.wait_calls, 3)
        self.assertEqual(sync_once.call_count, 3)
        sleep.assert_not_called()
        self.assertEqual(result["wait"], {"requested_seconds": 45, "reason": "interrupted", "settled": False})

    def test_no_active_request_syncs_once_without_sleep(self):
        clock = FakeClock()
        with patch.object(self.service, "_sync_once", wraps=self.service._sync_once) as sync_once, \
                patch("ao_project_room.time.sleep", side_effect=clock.sleep) as sleep, \
                patch("ao_project_room.time.monotonic", side_effect=clock.monotonic):
            result = self.service.ao_room_sync(self.room, wait_seconds=45)
        self.assertEqual(sync_once.call_count, 1)
        self.assertEqual(clock.sleeps, [])
        sleep.assert_not_called()
        self.assertEqual(result["wait"], {"requested_seconds": 45, "reason": "no_active_request", "settled": True})

    def test_invalid_wait_values_refuse_before_ao_get_or_state_write(self):
        self.prepare_running()
        before = self.state_path().read_bytes()
        gets = self.fake.gets
        for value in (-1, 45.01, True, float("nan"), float("inf"), "5"):
            with self.subTest(value=value), self.assertRaisesRegex(ao.RoomError, "wait_seconds must be finite and between 0 and 45"):
                self.service.ao_room_sync(self.room, wait_seconds=value)
            self.assertEqual(self.fake.gets, gets)
            self.assertEqual(self.state_path().read_bytes(), before)

    def test_room_lock_is_released_during_each_poll_sleep(self):
        self.prepare_running()
        acquired = []

        def acquire_between_polls(_):
            with (self.service.root / ".lock").open("a+") as stream:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired.append(True)
                fcntl.flock(stream, fcntl.LOCK_UN)

        clock = FakeClock(on_sleep=acquire_between_polls)
        with patch("ao_project_room.time.sleep", side_effect=clock.sleep), \
                patch("ao_project_room.time.monotonic", side_effect=clock.monotonic):
            result = self.service.ao_room_sync(self.room, wait_seconds=45)
        self.assertEqual(result["wait"]["reason"], "timeout")
        self.assertEqual(len(acquired), len(clock.sleeps))
        self.assertGreater(len(acquired), 0)

    def test_room_error_on_second_sync_propagates(self):
        self.prepare_running()
        clock = FakeClock()
        original_request = self.fake.request
        session_gets = 0

        def fail_on_second_session_get(method, path, payload=None):
            nonlocal session_gets
            if method == "GET" and path == "/sessions/engineer":
                session_gets += 1
                if session_gets == 2:
                    raise ao.RoomError("Synthetic AO error on the second poll")
            return original_request(method, path, payload)

        with patch.object(self.fake, "request", side_effect=fail_on_second_session_get), \
                patch.object(self.service, "_sync_once", wraps=self.service._sync_once) as sync_once, \
                patch("ao_project_room.time.sleep", side_effect=clock.sleep), \
                patch("ao_project_room.time.monotonic", side_effect=clock.monotonic):
            with self.assertRaisesRegex(ao.RoomError, "Synthetic AO error on the second poll"):
                self.service.ao_room_sync(self.room, wait_seconds=45)
        self.assertEqual(sync_once.call_count, 2)
        self.assertEqual(session_gets, 2)

    def test_tools_list_and_cli_and_mcp_forward_wait_seconds(self):
        arguments = {"room_id": "synthetic-room", "wait_seconds": 5}
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            controller = project_room.Service(home)
            static_schema = copy.deepcopy(ao.TOOL_SCHEMAS["ao_room_sync"][1])
            listing = project_room_mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, controller)
            definition = next(tool for tool in listing["result"]["tools"] if tool["name"] == "ao_room_sync")
            self.assertEqual(definition["inputSchema"], ao.TOOL_SCHEMAS["ao_room_sync"][1])
            self.assertEqual(definition["inputSchema"]["properties"]["wait_seconds"],
                             {"type": "number", "minimum": 0, "maximum": 45})
            self.assertEqual(definition["inputSchema"]["required"], ["room_id"])
            config = controller.home / "ao" / "config.json"
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_text(json.dumps({"sync_wait_max_seconds": 600}))
            listing = project_room_mcp.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"}, controller)
            definition = next(tool for tool in listing["result"]["tools"] if tool["name"] == "ao_room_sync")
            self.assertEqual(definition["inputSchema"]["properties"]["wait_seconds"]["maximum"], 600)
            expected_schema = copy.deepcopy(static_schema)
            expected_schema["properties"]["wait_seconds"]["maximum"] = 600
            self.assertEqual(definition["inputSchema"], expected_schema)
            self.assertEqual(ao.TOOL_SCHEMAS["ao_room_sync"][1], static_schema)
            config.write_text(json.dumps({"sync_wait_max_seconds": 44}))
            listing = project_room_mcp.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/list"}, controller)
            definition = next(tool for tool in listing["result"]["tools"] if tool["name"] == "ao_room_sync")
            self.assertEqual(definition["inputSchema"]["properties"]["wait_seconds"]["maximum"], 45)
            envelope = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": "ao_room_sync", "arguments": arguments}}
            with patch.object(project_room.ao_project_room, "Service") as adapter:
                adapter.return_value.ao_room_sync.return_value = {"forwarded": True}
                response = project_room_mcp.handle(envelope, controller)
            self.assertFalse(response["result"]["isError"])
            adapter.return_value.ao_room_sync.assert_called_once_with(**arguments)

            output = StringIO()
            with patch.object(project_room.ao_project_room, "Service") as adapter, \
                    patch("project_room.signal.signal"), redirect_stdout(output):
                adapter.return_value.ao_room_sync.return_value = {"forwarded": True}
                exit_code = project_room.main([
                    "--home", str(Path(temporary) / "cli-home"), "call", "ao_room_sync",
                    "--args", json.dumps(arguments)
                ])
            self.assertEqual(exit_code, 0)
            self.assertEqual(json.loads(output.getvalue()), {"forwarded": True})
            adapter.return_value.ao_room_sync.assert_called_once_with(**arguments)


if __name__ == "__main__":
    unittest.main()
