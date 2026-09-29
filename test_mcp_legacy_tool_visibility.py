import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import project_room
import project_room_mcp


class LegacyToolVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / "project-room"

    def write_config(self, home, value):
        path = home / "ao" / "config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(value, str):
            path.write_text(value, encoding="utf-8")
        else:
            path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def service(self, home=None):
        return project_room.Service(self.home if home is None else home)

    def request(self, service, method, params=None):
        message = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params is not None:
            message["params"] = params
        return project_room_mcp.handle(message, service)["result"]

    def tool_names(self, service):
        return {tool["name"] for tool in self.request(service, "tools/list")["tools"]}

    def all_tool_names(self):
        return set(project_room.TOOL_SCHEMAS)

    def ao_tool_names(self):
        return {name for name in project_room.TOOL_SCHEMAS if name.startswith("ao_")}

    def add_legacy_room(self, service):
        with service.db() as db:
            db.execute(
                "INSERT INTO rooms VALUES(?,?,?,?,?)",
                ("legacy-room", "/tmp/project", "feature", "/tmp/legacy-room", "2026-01-01T00:00:00Z"),
            )

    def test_fresh_home_lists_all_tools_and_full_instructions(self):
        service = self.service()
        names = self.tool_names(service)
        initialized = self.request(service, "initialize", {"protocolVersion": "2025-03-26"})
        self.assertEqual(names, self.all_tool_names())
        self.assertEqual(len(names), len(project_room.TOOL_SCHEMAS))
        self.assertEqual(initialized["instructions"], project_room_mcp.INSTRUCTIONS)

    def test_ao_without_legacy_rooms_hides_tools_and_legacy_instructions(self):
        self.write_config(self.home, {"default_backend": "ao"})
        service = self.service()
        names = self.tool_names(service)
        initialized = self.request(service, "initialize", {"protocolVersion": "2025-03-26"})
        instructions = initialized["instructions"]
        self.assertEqual(names, self.ao_tool_names())
        self.assertEqual(len(names), len(project_room.TOOL_SCHEMAS) - 23)
        self.assertNotIn("legacy room_* rooms", instructions)
        self.assertTrue(instructions.endswith("The project-room skill supplies the workflow."))
        self.assertEqual(instructions, project_room_mcp.AO_INSTRUCTIONS + project_room_mcp.SKILL_INSTRUCTION)
        self.assertEqual(
            project_room_mcp.AO_INSTRUCTIONS + project_room_mcp.LEGACY_INSTRUCTIONS
            + project_room_mcp.SKILL_INSTRUCTION,
            project_room_mcp.INSTRUCTIONS,
        )

    def test_ao_with_a_legacy_room_keeps_all_tools(self):
        self.write_config(self.home, {"default_backend": "ao"})
        service = self.service()
        self.add_legacy_room(service)
        self.assertEqual(self.tool_names(service), self.all_tool_names())

    def test_explicit_legacy_tools_opt_in_keeps_all_tools(self):
        self.write_config(self.home, {"default_backend": "ao", "legacy_tools": True})
        self.assertEqual(self.tool_names(self.service()), self.all_tool_names())

    def test_unreadable_or_unexpected_config_keeps_all_tools(self):
        cases = [
            ("non-dict", []),
            ("invalid-json", "{invalid json"),
            ("legacy-backend", {"default_backend": "legacy"}),
        ]
        for name, config in cases:
            with self.subTest(name=name):
                home = self.base / name
                self.write_config(home, config)
                self.assertEqual(self.tool_names(self.service(home)), self.all_tool_names())

        home = self.base / "unreadable"
        self.write_config(home, {"default_backend": "ao"})
        service = self.service(home)
        with patch.object(Path, "read_text", side_effect=PermissionError("synthetic unreadable config")):
            self.assertEqual(self.tool_names(service), self.all_tool_names())

    def test_hidden_legacy_call_is_refused_without_calling_service_or_writing(self):
        self.write_config(self.home, {"default_backend": "ao"})
        service = self.service()
        project = self.base / "project"
        project.mkdir()
        before = {path.relative_to(self.home) for path in self.home.rglob("*")}
        with patch.object(service, "call") as call:
            result = self.request(
                service,
                "tools/call",
                {"name": "room_open", "arguments": {"project_path": str(project), "feature": "hidden"}},
            )
        expected_error = {
            "error": (
                'Legacy room_* tools are not exposed: default_backend is ao and no legacy room exists. '
                'Set "legacy_tools": true in PROJECT_ROOM_HOME/ao/config.json and restart the MCP server to use them.'
            )
        }
        self.assertTrue(result["isError"])
        self.assertEqual(result["content"][0]["text"], json.dumps(expected_error, ensure_ascii=False))
        call.assert_not_called()
        after = {path.relative_to(self.home) for path in self.home.rglob("*")}
        self.assertEqual(after, before)
        with service.db() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM rooms LIMIT 1").fetchone())

    def test_visibility_is_cached_for_the_service_lifetime(self):
        self.write_config(self.home, {"default_backend": "ao"})
        service = self.service()
        hidden_names = self.tool_names(service)
        self.assertEqual(hidden_names, self.ao_tool_names())
        self.add_legacy_room(service)
        self.assertEqual(self.tool_names(service), hidden_names)
        self.assertEqual(self.tool_names(self.service()), self.all_tool_names())

    def test_every_legacy_schema_is_a_room_tool_and_there_are_23(self):
        legacy_names = {name for name in project_room.TOOL_SCHEMAS if not name.startswith("ao_")}
        self.assertEqual(len(legacy_names), 23)
        self.assertTrue(all(name.startswith("room_") for name in legacy_names))


if __name__ == "__main__":
    unittest.main()
