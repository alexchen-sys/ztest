import unittest


class PyparsingLike(unittest.TestCase):
    def runTest(self):
        self.assertTrue(True)


class WithReal(unittest.TestCase):
    def runTest(self):
        self.assertTrue(False)  # must not be collected: explicit test_* methods exist

    def test_real(self):
        pass
