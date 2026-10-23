import unittest

import pytest


# an autouse fixture on a unittest-class method
# receives self = the test instance (not a LookupError "fixture 'self' not found").
class TestSignatures(unittest.TestCase):
    @pytest.fixture(autouse=True)
    def init(self):
        self.marker = "bound-%s" % type(self).__name__

    def test_for(self):
        self.assertEqual(self.marker, "bound-TestSignatures")

    def test_second(self):
        self.assertTrue(self.marker.startswith("bound-"))
