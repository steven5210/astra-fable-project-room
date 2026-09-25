"""Diagnostic import failures must not render arbitrary exception content."""
import builtins
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import project_room


class CliErrorTests(unittest.TestCase):
    def test_import_failure_is_closed_before_service_or_evidence_access(self):
        original = builtins.__import__

        def fail_audit_import(name, *args, **kwargs):
            if name == "ao_evidence_audit":
                raise ImportError("PRIVATE_IMPORT_ERROR_SENTINEL")
            return original(name, *args, **kwargs)

        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "missing-home"
            output, error = io.StringIO(), io.StringIO()
            with mock.patch.object(builtins, "__import__", side_effect=fail_audit_import), \
                 mock.patch.object(project_room, "Service") as service, \
                 mock.patch.object(project_room.signal, "signal"), \
                 contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
                result = project_room.main(["ao-evidence-read-audit", "--home", str(home),
                    "--room", "room-synthetic", "--request", "request-synthetic",
                    "--ao-database", str(Path(temp) / "missing.db"),
                    "--evidence-root", str(Path(temp) / "missing-evidence")])
            self.assertEqual(result, 2)
            self.assertEqual(output.getvalue(), "")
            self.assertEqual(json.loads(error.getvalue()), {"error": "ao_evidence_read_audit_failed"})
            self.assertNotIn("PRIVATE_IMPORT_ERROR_SENTINEL", error.getvalue())
            service.assert_not_called()
            self.assertFalse(home.exists())
            self.assertEqual(list(Path(temp).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
