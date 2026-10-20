import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import metrics  # noqa: E402


class SummarizeTests(unittest.TestCase):
    def test_summary(self):
        self.assertEqual(metrics.summarize([1, 2, 3]), {"count": 3, "mean": 2.0, "min": 1, "max": 3})

    def test_rejects_empty(self):
        with self.assertRaises(ValueError):
            metrics.summarize([])


if __name__ == "__main__":
    unittest.main()
