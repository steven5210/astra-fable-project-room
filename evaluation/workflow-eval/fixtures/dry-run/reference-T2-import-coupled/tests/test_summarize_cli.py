"""Exercises summarize_cli.main in-process: an entry point missing ``main`` fails at import."""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from summarize_cli import main  # noqa: E402


class CliTests(unittest.TestCase):
    def run_cli(self, text):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "in.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
            out = os.path.join(directory, "out.txt")
            saved = sys.stdout
            try:
                sys.stdout = open(out, "w", encoding="utf-8")
                code = main([os.path.join(ROOT, "summarize_cli.py"), path])
            finally:
                sys.stdout.close()
                sys.stdout = saved
            with open(out, "r", encoding="utf-8") as handle:
                return code, handle.read()

    def test_valid(self):
        code, out = self.run_cli("1\n2.5\n\n4\n")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"count": 3, "max": 4.0, "mean": 2.5, "min": 1.0})

    def test_invalid_line(self):
        code, out = self.run_cli("1\nx\n")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()
