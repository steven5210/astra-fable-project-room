import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from upstream import verdict  # noqa: E402


class VerdictTests(unittest.TestCase):
    def test_precedence(self):
        self.assertEqual(verdict.summarize_statuses(["pass", "fail", "pending"])["verdict"], "FAIL")
        self.assertEqual(verdict.summarize_statuses(["pass", "not_run"])["verdict"], "INCOMPLETE")
        self.assertEqual(verdict.summarize_statuses(["pass"])["verdict"], "PASS")

    def test_invalid(self):
        with self.assertRaises(verdict.InvalidStatusesError):
            verdict.summarize_statuses(["PASS"])


if __name__ == "__main__":
    unittest.main()
