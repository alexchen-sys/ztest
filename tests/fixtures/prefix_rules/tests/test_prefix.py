import unittest


def testfoo():
    # pytest default python_functions=["test"] uses prefix matching
    pass


def test_underscore():
    pass


def atest_x():
    raise RuntimeError("must not be collected: no 'test' prefix")


class TestProbe:
    def tested(self):
        # prefix "test" without underscore - pytest collects methods too
        pass

    def helper(self):
        raise RuntimeError("must not be collected")


class CamelCase(unittest.TestCase):
    def testFoo(self):
        pass

    def testBar(self):
        pass
