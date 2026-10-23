import unittest


class TestSubSkip(unittest.TestCase):
    def test_mixed(self):
        # skipTest inside subTest - the parent is passed,
        # each skipped subtest adds +1 skipped (pytest-subtests parity).
        for i in range(3):
            with self.subTest(i=i):
                if i == 1:
                    self.skipTest(f"no {i}")
                self.assertTrue(True)
