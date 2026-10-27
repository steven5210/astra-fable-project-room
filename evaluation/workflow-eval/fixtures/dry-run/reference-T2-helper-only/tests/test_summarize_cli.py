import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import helper  # noqa: E402


class HelperOnlyTests(unittest.TestCase):
    """Proves the helper, never the command-line entry point: the probe must flag this."""

    def test_summary(self):
        self.assertEqual(helper.summarize([1.0, 2.5, 4.0]), {"count": 3, "mean": 2.5, "min": 1.0, "max": 4.0})


if __name__ == "__main__":
    unittest.main()
