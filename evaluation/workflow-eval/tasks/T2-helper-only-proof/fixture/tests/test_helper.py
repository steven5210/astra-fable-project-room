import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import helper  # noqa: E402


class HelperTests(unittest.TestCase):
    def test_summary(self):
        self.assertEqual(helper.summarize([2, 4]), {"count": 2, "mean": 3.0, "min": 2, "max": 4})


if __name__ == "__main__":
    unittest.main()
