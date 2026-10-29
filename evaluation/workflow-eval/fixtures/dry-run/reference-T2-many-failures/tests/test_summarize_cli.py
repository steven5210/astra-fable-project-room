"""Six CLI assertions; under the stub each fails with a long named message a small tail cannot hold."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXPECTED = {"count": 3, "max": 4.0, "mean": 2.5, "min": 1.0}


class CliTests(unittest.TestCase):
    def run_cli(self, text):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "in.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
            return subprocess.run([sys.executable, "-B", os.path.join(ROOT, "summarize_cli.py"), path],
                                  capture_output=True, text=True)


def _case(index):
    def run(self):
        result = self.run_cli("1\n2.5\n\n4\n")
        self.assertEqual(json.loads(result.stdout or "{}"), EXPECTED,
                         "case-%02d failed against the production entry point; " % index
                         + "diagnostic padding " * 12 + "for case %02d" % index)
    return run


for _index in range(6):
    setattr(CliTests, "test_case_%02d" % _index, _case(_index))
del _index

if __name__ == "__main__":
    unittest.main()
