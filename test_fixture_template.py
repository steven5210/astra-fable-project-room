"""Order-controlled checks of the TEMPLATE_SETUP machinery: a templated class builds its room
workflow once into a class-owned snapshot, then every test restores pristine disk bytes at the same
paths and fresh in-memory state from the snapshot."""
import subprocess
import unittest

from test_ao_acceptance_extension import AcceptanceContinuationFixture


def head(repo):
    return subprocess.run(['git', '-C', str(repo), 'rev-parse', 'HEAD'],
                          check=True, capture_output=True, text=True).stdout.strip()


class FixtureTemplateTests(unittest.TestCase):
    def test_disk_and_memory_restore_to_the_class_template(self):
        class Probe(AcceptanceContinuationFixture):
            def test_a_mutates(self):
                type(self).template_head = head(self.repo)
                type(self).template_events = list(self.native_events)
                (self.root / 'stray-file').write_text('stray\n')
                subprocess.run(['git', '-C', str(self.repo), 'commit', '--allow-empty', '-m', 'mutation'],
                               check=True, capture_output=True)
                self.fake.snapshots['engineer']['controller'] = 'mutated'
                self.native_events.append({'type': 'user', 'synthetic': True})

            def test_b_restored(self):
                self.assertFalse((self.root / 'stray-file').exists())
                self.assertEqual(head(self.repo), type(self).template_head)
                self.assertEqual(self.fake.snapshots['engineer']['controller'], 'ready')
                self.assertEqual(self.native_events, type(self).template_events)

        suite = unittest.TestSuite([Probe('test_a_mutates'), Probe('test_b_restored')])
        result = unittest.TestResult()
        suite.run(result)
        self.assertEqual(result.testsRun, 2)
        self.assertFalse(result.errors, [entry[1] for entry in result.errors])
        self.assertFalse(result.failures, [entry[1] for entry in result.failures])

    def test_snapshot_assertion_flags_an_uncovered_attribute(self):
        class Stray(AcceptanceContinuationFixture):
            def build_template(self):
                super().build_template()
                self.stray = 1

            def test_method(self):
                pass

        result = unittest.TestResult()
        unittest.TestSuite([Stray('test_method')]).run(result)
        self.assertEqual(result.testsRun, 1)
        problems = ''.join(entry[1] for entry in result.errors + result.failures)
        self.assertTrue(problems, 'setUp must surface the uncovered-attribute assertion')
        self.assertIn('stray', problems)

    def test_template_is_bound_to_the_exact_class(self):
        class ThreeReviews(AcceptanceContinuationFixture):
            review_count = 3

            def test_method(self):
                pass

        class TwoReviews(AcceptanceContinuationFixture):
            review_count = 2

            def test_method(self):
                pass

        suite = unittest.TestSuite([ThreeReviews('test_method'), TwoReviews('test_method')])
        result = unittest.TestResult()
        suite.run(result)
        self.assertFalse(result.errors, [entry[1] for entry in result.errors])
        self.assertFalse(result.failures, [entry[1] for entry in result.failures])
        three = ThreeReviews._template
        two = TwoReviews._template
        self.assertIsNot(three, two)
        self.assertNotEqual(len(three['state']['fake']['snapshots']['reviewer']['turns']),
                            len(two['state']['fake']['snapshots']['reviewer']['turns']))
        self.assertNotEqual(three['state']['attrs']['room'], two['state']['attrs']['room'])


if __name__ == '__main__':
    unittest.main()
