"""A subprocess call on another file: the entry point is exercised only in-process."""
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

SCRIPT = "summarize_cli.py"
from summarize_cli import main  # noqa: E402


class CliTests(unittest.TestCase):
    def test_unrelated_script(self):
        result = subprocess.run([sys.executable, "-B", os.path.join(ROOT, "other.py")],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "ok")

    def test_main_in_process(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "in.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("1\n2.5\n\n4\n")
            self.assertEqual(main([SCRIPT, path]), 0)


if __name__ == "__main__":
    unittest.main()
