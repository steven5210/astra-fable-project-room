import json
import os
import subprocess as sp
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class CliTests(unittest.TestCase):
    def run_cli(self, text):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "in.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
            return sp.run([sys.executable, "-B", os.path.join(ROOT, "summarize_cli.py"), path],
                          capture_output=True, text=True)

    def test_valid(self):
        result = self.run_cli("1\n2.5\n\n4\n")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), {"count": 3, "max": 4.0, "mean": 2.5, "min": 1.0})

    def test_invalid_line(self):
        result = self.run_cli("1\nx\n")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
