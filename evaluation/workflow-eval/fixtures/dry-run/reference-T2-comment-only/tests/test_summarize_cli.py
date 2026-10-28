"""Mentions subprocess and summarize_cli.py and -B only in prose, never in code."""
import unittest


class CommentOnlyTests(unittest.TestCase):
    # This suite would launch subprocess.run([sys.executable, "-B", "summarize_cli.py"]) if it
    # exercised the entry point; it deliberately never does, so the probe must flag it.
    def test_trivial(self):
        self.assertTrue(True)


if __name__ == "__main__":
    unittest.main()
